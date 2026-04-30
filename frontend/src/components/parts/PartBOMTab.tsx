import React, { useEffect, useState, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../../services/api';
import { Part, PartType } from '../../types';
import {
  BOM, BOMItem, LineType, lineTypeColors, lineTypeLabels,
} from '../../types/engineering';
import { useToast } from '../ui/Toast';
import { StatusBadge } from '../ui/StatusBadge';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { BOMImportWizard } from './BOMImportWizard';
import {
  PlusIcon,
  TrashIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  DocumentDuplicateIcon,
  ArrowUpTrayIcon,
  CheckCircleIcon,
  ArrowUturnLeftIcon,
  MagnifyingGlassIcon,
} from '@heroicons/react/24/outline';

type BatchPartRow = {
  id: string;
  item_number: number;
  part_number: string;
  revision: string;
  name: string;
  description: string;
  part_type: PartType;
  line_type: LineType;
  quantity: string;
  notes: string;
};

interface Props {
  part: Part;
  bom: BOM | null;
  onBOMChanged: () => Promise<void>;
}

export function PartBOMTab({ part, bom, onBOMChanged }: Props) {
  const navigate = useNavigate();
  const { showToast } = useToast();

  // View state
  const [viewMode, setViewMode] = useState<'single' | 'exploded'>('single');
  const [explodedView, setExplodedView] = useState<BOMItem[]>([]);
  const [expandedItems, setExpandedItems] = useState<Set<number>>(new Set());

  // Add item state
  const [showAddItem, setShowAddItem] = useState(false);
  const [showNewPart, setShowNewPart] = useState(false);
  const [showBatchAdd, setShowBatchAdd] = useState(false);
  const [batchRows, setBatchRows] = useState<BatchPartRow[]>([]);
  const [batchSubmitting, setBatchSubmitting] = useState(false);
  const [allParts, setAllParts] = useState<Part[]>([]);
  const [partSearch, setPartSearch] = useState('');
  const [newItem, setNewItem] = useState({
    component_part_id: 0,
    item_number: 10,
    quantity: 1,
    item_type: 'make' as const,
    line_type: 'component' as LineType,
    find_number: '',
    scrap_factor: 0,
    is_optional: false,
    notes: '',
    torque_spec: '',
    installation_notes: '',
  });
  const [newPart, setNewPart] = useState({
    part_number: '',
    name: '',
    part_type: 'manufactured' as PartType,
    revision: 'A',
    description: '',
  });

  // Import state
  const [showImport, setShowImport] = useState(false);

  // Confirm state
  const [confirmAction, setConfirmAction] = useState<{ title: string; message: string; action: () => void } | null>(null);

  // Create BOM state
  const [creating, setCreating] = useState(false);

  // Load exploded view when switching
  useEffect(() => {
    if (viewMode === 'exploded' && bom) {
      api.explodeBOM(bom.id).then(setExplodedView).catch(() => setExplodedView([]));
    }
  }, [viewMode, bom]);

  // Load parts for add-item dropdown
  useEffect(() => {
    if (showAddItem && allParts.length === 0) {
      api.getParts({}).then(setAllParts).catch(() => {});
    }
  }, [showAddItem, allParts.length]);

  // Update default item_number when BOM changes
  useEffect(() => {
    if (bom) {
      const maxItem = bom.items.reduce((max, i) => Math.max(max, i.item_number), 0);
      setNewItem(prev => ({ ...prev, item_number: maxItem + 10 }));
    }
  }, [bom]);

  const filteredParts = useMemo(() => {
    const search = partSearch.trim().toLowerCase();
    const candidateParts = allParts.filter(p => p.id !== part.id);
    if (!search) return candidateParts.slice(0, 50);
    return candidateParts
      .filter(p =>
        p.part_number.toLowerCase().includes(search) ||
        p.name.toLowerCase().includes(search)
      )
      .slice(0, 50);
  }, [allParts, part.id, partSearch]);

  const nextItemNumber = () => {
    if (!bom) return 10;
    return bom.items.reduce((max, i) => Math.max(max, i.item_number), 0) + 10;
  };

  const createBatchRow = (itemNumber: number): BatchPartRow => ({
    id: `${Date.now()}-${itemNumber}-${Math.random().toString(36).slice(2)}`,
    item_number: itemNumber,
    part_number: '',
    revision: 'A',
    name: '',
    description: '',
    part_type: 'manufactured',
    line_type: 'component',
    quantity: '1',
    notes: '',
  });

  const openBatchAdd = () => {
    const firstItemNumber = nextItemNumber();
    setBatchRows([
      createBatchRow(firstItemNumber),
      createBatchRow(firstItemNumber + 10),
      createBatchRow(firstItemNumber + 20),
    ]);
    setShowBatchAdd(true);
  };

  const addBatchRow = () => {
    setBatchRows(prev => {
      const nextNumber = prev.length > 0
        ? Math.max(...prev.map(row => row.item_number)) + 10
        : nextItemNumber();
      return [...prev, createBatchRow(nextNumber)];
    });
  };

  const removeBatchRow = (rowId: string) => {
    setBatchRows(prev => prev.filter(row => row.id !== rowId));
  };

  const updateBatchRow = <K extends keyof BatchPartRow>(rowId: string, field: K, value: BatchPartRow[K]) => {
    setBatchRows(prev => prev.map(row => (
      row.id === rowId ? { ...row, [field]: value } : row
    )));
  };

  // ── Actions ────────────────────────────────────────────────────────────

  const handleCreateBOM = async () => {
    setCreating(true);
    try {
      await api.createBOM({ part_id: part.id, revision: part.revision, bom_type: 'standard' });
      await onBOMChanged();
      showToast('success', 'BOM created');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create BOM');
    } finally {
      setCreating(false);
    }
  };

  const handleAddItem = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!bom || newItem.component_part_id <= 0) {
      showToast('error', 'Select a component part');
      return;
    }
    try {
      await api.addBOMItem(bom.id, newItem);
      await onBOMChanged();
      showToast('success', 'Item added');
      setShowAddItem(false);
      setPartSearch('');
      setNewItem(prev => ({
        ...prev,
        component_part_id: 0,
        item_number: prev.item_number + 10,
        quantity: 1,
        notes: '',
        torque_spec: '',
        installation_notes: '',
      }));
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to add item');
    }
  };

  const handleCreateNewPart = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const createdPart = await api.createPart(newPart);
      setAllParts(prev => [...prev, createdPart]);
      setNewItem(prev => ({ ...prev, component_part_id: createdPart.id }));
      setPartSearch(`${createdPart.part_number} - ${createdPart.name}`);
      setShowNewPart(false);
      setNewPart({
        part_number: '',
        name: '',
        part_type: 'manufactured',
        revision: 'A',
        description: '',
      });
      showToast('success', `Part ${createdPart.part_number} created`);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create part');
    }
  };

  const handleCreateBatchParts = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!bom) return;

    const rowsToCreate = batchRows
      .map(row => ({
        ...row,
        part_number: row.part_number.trim().toUpperCase(),
        revision: row.revision.trim().toUpperCase() || 'A',
        name: row.name.trim(),
        description: row.description.trim(),
        quantity: Number(row.quantity),
        notes: row.notes.trim(),
      }))
      .filter(row => row.part_number || row.name);

    if (rowsToCreate.length === 0) {
      showToast('error', 'Add at least one part row');
      return;
    }

    const incompleteRow = rowsToCreate.find(row =>
      !row.part_number ||
      !row.name ||
      !Number.isFinite(row.quantity) ||
      row.quantity <= 0 ||
      row.item_number <= 0
    );
    if (incompleteRow) {
      showToast('error', 'Each part row needs a part number, name, item number, and quantity');
      return;
    }

    const duplicatePartNumber = rowsToCreate.find((row, index) =>
      rowsToCreate.findIndex(other => other.part_number === row.part_number) !== index
    );
    if (duplicatePartNumber) {
      showToast('error', `Duplicate part number: ${duplicatePartNumber.part_number}`);
      return;
    }

    setBatchSubmitting(true);
    try {
      const createdParts: Part[] = [];
      for (const row of rowsToCreate) {
        const createdPart = await api.createPart({
          part_number: row.part_number,
          revision: row.revision,
          name: row.name,
          description: row.description || undefined,
          part_type: row.part_type,
          unit_of_measure: 'each',
        });

        createdParts.push(createdPart);

        await api.addBOMItem(bom.id, {
          component_part_id: createdPart.id,
          item_number: row.item_number,
          quantity: row.quantity,
          item_type: row.part_type === 'purchased' || row.part_type === 'hardware' || row.part_type === 'consumable' ? 'buy' : 'make',
          line_type: row.line_type,
          notes: row.notes || undefined,
        });
      }

      setAllParts(prev => [...prev, ...createdParts]);
      await onBOMChanged();
      setShowBatchAdd(false);
      setBatchRows([]);
      showToast('success', `${createdParts.length} part${createdParts.length !== 1 ? 's' : ''} created and added`);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create and add parts');
    } finally {
      setBatchSubmitting(false);
    }
  };

  const handleDeleteItem = (itemId: number) => {
    setConfirmAction({
      title: 'Delete BOM Item',
      message: 'Remove this item from the BOM?',
      action: async () => {
        try {
          await api.deleteBOMItem(itemId);
          await onBOMChanged();
          showToast('success', 'Item removed');
        } catch (err: any) {
          showToast('error', err.response?.data?.detail || 'Failed to delete item');
        }
      },
    });
  };

  const handleRelease = async () => {
    if (!bom) return;
    try {
      await api.releaseBOM(bom.id);
      await onBOMChanged();
      showToast('success', 'BOM released');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to release BOM');
    }
  };

  const handleUnrelease = async () => {
    if (!bom) return;
    try {
      await api.unreleaseBOM(bom.id);
      await onBOMChanged();
      showToast('success', 'BOM returned to draft');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to unrelease BOM');
    }
  };

  const handleDeleteBOM = () => {
    if (!bom) return;
    setConfirmAction({
      title: 'Delete BOM',
      message: 'This will permanently delete the BOM and all its items. This cannot be undone.',
      action: async () => {
        try {
          await api.deleteBOM(bom.id);
          await onBOMChanged();
          showToast('success', 'BOM deleted');
        } catch (err: any) {
          showToast('error', err.response?.data?.detail || 'Failed to delete BOM');
        }
      },
    });
  };

  const toggleExpanded = (itemId: number) => {
    setExpandedItems(prev => {
      const next = new Set(prev);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  };

  // ── No BOM State ───────────────────────────────────────────────────────

  if (!bom) {
    return (
      <div className="card text-center py-12">
        <DocumentDuplicateIcon className="h-12 w-12 mx-auto text-slate-600 mb-4" />
        <h3 className="text-lg font-medium text-white mb-2">No Bill of Materials</h3>
        <p className="text-sm text-slate-400 mb-6 max-w-md mx-auto">
          Create a BOM to define the components, hardware, and materials needed to build this part.
        </p>
        <div className="flex justify-center gap-3">
          <button onClick={handleCreateBOM} disabled={creating} className="btn-primary flex items-center gap-2">
            <PlusIcon className="h-4 w-4" />
            {creating ? 'Creating...' : 'Create BOM'}
          </button>
          <button onClick={() => setShowImport(true)} className="btn-secondary flex items-center gap-2">
            <ArrowUpTrayIcon className="h-4 w-4" />
            Import from Document
          </button>
        </div>
        {showImport && (
          <BOMImportWizard
            onComplete={async () => {
              await onBOMChanged();
              setShowImport(false);
            }}
            onClose={() => setShowImport(false)}
          />
        )}
      </div>
    );
  }

  // ── BOM Exists ─────────────────────────────────────────────────────────

  return (
    <div className="space-y-4">
      {/* Header Bar */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div className="flex items-center gap-3">
          <StatusBadge status={bom.status} />
          <span className="text-sm text-slate-400">Rev {bom.revision}</span>
          <span className="text-sm text-slate-400">{bom.items.length} item{bom.items.length !== 1 ? 's' : ''}</span>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {/* View Toggle */}
          <div className="flex rounded-lg border border-slate-600 overflow-hidden">
            <button
              onClick={() => setViewMode('single')}
              className={`px-3 py-1.5 text-xs font-medium ${viewMode === 'single' ? 'bg-werco-navy-600 text-white' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}
            >
              Single Level
            </button>
            <button
              onClick={() => setViewMode('exploded')}
              className={`px-3 py-1.5 text-xs font-medium ${viewMode === 'exploded' ? 'bg-werco-navy-600 text-white' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}
            >
              Multi-Level
            </button>
          </div>

          {bom.status === 'draft' && (
            <>
              <button onClick={() => setShowAddItem(true)} className="btn-secondary flex items-center gap-1 text-sm">
                <PlusIcon className="h-4 w-4" />
                Add Item
              </button>
              <button onClick={openBatchAdd} className="btn-secondary flex items-center gap-1 text-sm">
                <PlusIcon className="h-4 w-4" />
                Batch Add Parts
              </button>
              <button onClick={() => setShowImport(true)} className="btn-secondary flex items-center gap-1 text-sm">
                <ArrowUpTrayIcon className="h-4 w-4" />
                Import
              </button>
              <button onClick={handleRelease} className="btn-success text-sm">
                <CheckCircleIcon className="h-4 w-4 mr-1 inline" />
                Release
              </button>
              <button onClick={handleDeleteBOM} className="btn-danger text-sm">
                <TrashIcon className="h-4 w-4" />
              </button>
            </>
          )}
          {bom.status === 'released' && (
            <button onClick={handleUnrelease} className="btn-secondary flex items-center gap-1 text-sm">
              <ArrowUturnLeftIcon className="h-4 w-4" />
              Unrelease
            </button>
          )}
        </div>
      </div>

      {/* Items Table */}
      <div className="card overflow-hidden p-0">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-700">
            <thead className="bg-slate-800">
              <tr>
                {viewMode === 'exploded' && <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Level</th>}
                <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Item #</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Part</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Category</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Qty</th>
                {viewMode === 'exploded' && <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Ext Qty</th>}
                <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">UOM</th>
                {viewMode === 'single' && bom.status === 'draft' && (
                  <th className="px-4 py-3 w-16" />
                )}
              </tr>
            </thead>
            <tbody className="bg-[#151b28] divide-y divide-slate-700">
              {viewMode === 'single' ? (
                bom.items.length > 0 ? (
                  bom.items.map(item => (
                    <SingleLevelRow
                      key={item.id}
                      item={item}
                      isDraft={bom.status === 'draft'}
                      onDelete={handleDeleteItem}
                      onNavigate={(partId) => navigate(`/parts/${partId}?tab=bom`)}
                    />
                  ))
                ) : (
                  <tr>
                    <td colSpan={7} className="py-12 text-center text-slate-400">
                      No items yet. Add components to build up this BOM.
                    </td>
                  </tr>
                )
              ) : (
                explodedView.length > 0 ? (
                  explodedView.map(item => (
                    <ExplodedRow
                      key={`${item.id}-${item.level}`}
                      item={item}
                      depth={0}
                      expandedItems={expandedItems}
                      onToggle={toggleExpanded}
                    />
                  ))
                ) : (
                  <tr>
                    <td colSpan={7} className="py-12 text-center text-slate-400">
                      No items to display.
                    </td>
                  </tr>
                )
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Add Item Modal */}
      {showAddItem && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setShowAddItem(false)}>
          <div className="bg-[#151b28] rounded-xl p-6 max-w-lg w-full mx-4 shadow-xl animate-scale-in" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between gap-3 mb-4">
              <h3 className="text-lg font-semibold text-white">Add BOM Item</h3>
              <button
                type="button"
                onClick={() => setShowNewPart(true)}
                className="btn-secondary btn-sm flex items-center gap-1"
                title="Create a new component part"
              >
                <PlusIcon className="h-4 w-4" />
                New Part
              </button>
            </div>
            <form onSubmit={handleAddItem} className="space-y-4">
              {/* Part Search */}
              <div>
                <label className="label">Component Part</label>
                <div className="relative">
                  <MagnifyingGlassIcon className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
                  <input
                    type="text"
                    value={partSearch}
                    onChange={e => setPartSearch(e.target.value)}
                    placeholder="Search by part number or name..."
                    className="input pl-9"
                    autoFocus
                  />
                </div>
                {partSearch && (
                  <div className="mt-1 max-h-40 overflow-y-auto border border-slate-700 rounded-lg">
                    {filteredParts.map(p => (
                      <button
                        key={p.id}
                        type="button"
                        onClick={() => {
                          setNewItem(prev => ({ ...prev, component_part_id: p.id }));
                          setPartSearch(`${p.part_number} - ${p.name}`);
                        }}
                        className={`w-full text-left px-3 py-2 text-sm hover:bg-slate-700/50 ${
                          p.id === newItem.component_part_id ? 'bg-blue-500/10' : ''
                        }`}
                      >
                        <span className="font-medium text-slate-200">{p.part_number}</span>
                        <span className="text-slate-400 ml-2">{p.name}</span>
                      </button>
                    ))}
                    {filteredParts.length === 0 && (
                      <div className="px-3 py-3 text-sm text-slate-400 text-center">
                        No parts found.
                      </div>
                    )}
                  </div>
                )}
                {newItem.component_part_id > 0 && (
                  <p className="text-xs text-green-600 mt-1">
                    Selected: {allParts.find(p => p.id === newItem.component_part_id)?.part_number}
                  </p>
                )}
              </div>

              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="label">Item #</label>
                  <input
                    type="number"
                    value={newItem.item_number}
                    onChange={e => setNewItem(prev => ({ ...prev, item_number: parseInt(e.target.value) || 0 }))}
                    className="input"
                    step={1}
                  />
                </div>
                <div>
                  <label className="label">Quantity</label>
                  <input
                    type="number"
                    value={newItem.quantity}
                    onChange={e => setNewItem(prev => ({ ...prev, quantity: parseFloat(e.target.value) || 0 }))}
                    className="input"
                    step="1"
                    min="0"
                  />
                </div>
                <div>
                  <label className="label">Line Type</label>
                  <select
                    value={newItem.line_type}
                    onChange={e => setNewItem(prev => ({ ...prev, line_type: e.target.value as LineType }))}
                    className="input"
                  >
                    <option value="component">Component</option>
                    <option value="hardware">Hardware</option>
                    <option value="consumable">Consumable</option>
                    <option value="reference">Reference</option>
                  </select>
                </div>
              </div>

              {newItem.line_type === 'hardware' && (
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="label">Torque Spec</label>
                    <input
                      type="text"
                      value={newItem.torque_spec}
                      onChange={e => setNewItem(prev => ({ ...prev, torque_spec: e.target.value }))}
                      className="input"
                      placeholder="e.g., 25 ft-lbs"
                    />
                  </div>
                  <div>
                    <label className="label">Install Notes</label>
                    <input
                      type="text"
                      value={newItem.installation_notes}
                      onChange={e => setNewItem(prev => ({ ...prev, installation_notes: e.target.value }))}
                      className="input"
                    />
                  </div>
                </div>
              )}

              <div>
                <label className="label">Notes</label>
                <textarea
                  value={newItem.notes}
                  onChange={e => setNewItem(prev => ({ ...prev, notes: e.target.value }))}
                  className="input"
                  rows={2}
                />
              </div>

              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={() => setShowAddItem(false)} className="btn-secondary">
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

      {/* Batch Add Parts Modal */}
      {showBatchAdd && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setShowBatchAdd(false)}>
          <div className="bg-[#151b28] rounded-xl p-6 max-w-6xl w-full mx-4 shadow-xl animate-scale-in max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
            <div className="flex items-start justify-between gap-4 mb-4">
              <div>
                <h3 className="text-lg font-semibold text-white">Create Parts and Add to BOM</h3>
                <p className="text-sm text-slate-400 mt-1">
                  Enter new component parts below. Each completed row will be created as a part and added to this assembly BOM.
                </p>
              </div>
              <button
                type="button"
                onClick={addBatchRow}
                className="btn-secondary btn-sm flex items-center gap-1 shrink-0"
              >
                <PlusIcon className="h-4 w-4" />
                Add Row
              </button>
            </div>

            <form onSubmit={handleCreateBatchParts} className="space-y-4">
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-slate-700">
                  <thead className="bg-slate-800">
                    <tr>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase w-24">Item #</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase min-w-44">Part Number</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase w-24">Rev</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase min-w-56">Name</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase min-w-48">Description</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase min-w-40">Type</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase min-w-36">Line</th>
                      <th className="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase min-w-36">Qty</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase min-w-44">Notes</th>
                      <th className="px-3 py-2 w-12" />
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700">
                    {batchRows.map((row, index) => (
                      <tr key={row.id}>
                        <td className="px-3 py-2 align-top">
                          <input
                            type="number"
                            value={row.item_number}
                            onChange={e => updateBatchRow(row.id, 'item_number', parseInt(e.target.value) || 0)}
                            className="input py-1.5 px-2 text-sm"
                            step={1}
                            min={10}
                          />
                        </td>
                        <td className="px-3 py-2 align-top">
                          <input
                            type="text"
                            value={row.part_number}
                            onChange={e => updateBatchRow(row.id, 'part_number', e.target.value.toUpperCase())}
                            className="input py-1.5 px-2 text-sm"
                            placeholder="Part number"
                            autoFocus={index === 0}
                          />
                        </td>
                        <td className="px-3 py-2 align-top">
                          <input
                            type="text"
                            value={row.revision}
                            onChange={e => updateBatchRow(row.id, 'revision', e.target.value.toUpperCase())}
                            className="input py-1.5 px-2 text-sm"
                          />
                        </td>
                        <td className="px-3 py-2 align-top">
                          <input
                            type="text"
                            value={row.name}
                            onChange={e => updateBatchRow(row.id, 'name', e.target.value)}
                            className="input py-1.5 px-2 text-sm"
                            placeholder="Part name"
                          />
                        </td>
                        <td className="px-3 py-2 align-top">
                          <input
                            type="text"
                            value={row.description}
                            onChange={e => updateBatchRow(row.id, 'description', e.target.value)}
                            className="input py-1.5 px-2 text-sm"
                            placeholder="Optional"
                          />
                        </td>
                        <td className="px-3 py-2 align-top">
                          <select
                            value={row.part_type}
                            onChange={e => updateBatchRow(row.id, 'part_type', e.target.value as PartType)}
                            className="input py-1.5 px-2 text-sm"
                          >
                            <option value="manufactured">Manufactured</option>
                            <option value="purchased">Purchased</option>
                            <option value="assembly">Assembly</option>
                            <option value="raw_material">Raw Material</option>
                            <option value="hardware">Hardware</option>
                            <option value="consumable">Consumable</option>
                          </select>
                        </td>
                        <td className="px-3 py-2 align-top">
                          <select
                            value={row.line_type}
                            onChange={e => updateBatchRow(row.id, 'line_type', e.target.value as LineType)}
                            className="input py-1.5 px-2 text-sm"
                          >
                            <option value="component">Component</option>
                            <option value="hardware">Hardware</option>
                            <option value="consumable">Consumable</option>
                            <option value="reference">Reference</option>
                          </select>
                        </td>
                        <td className="px-3 py-2 align-top">
                          <input
                            type="number"
                            value={row.quantity}
                            onChange={e => updateBatchRow(row.id, 'quantity', e.target.value)}
                            className="input w-32 py-1.5 px-3 text-sm text-right"
                            step="1"
                            min="0"
                          />
                        </td>
                        <td className="px-3 py-2 align-top">
                          <input
                            type="text"
                            value={row.notes}
                            onChange={e => updateBatchRow(row.id, 'notes', e.target.value)}
                            className="input py-1.5 px-2 text-sm"
                            placeholder="Optional"
                          />
                        </td>
                        <td className="px-3 py-2 align-top text-right">
                          <button
                            type="button"
                            onClick={() => removeBatchRow(row.id)}
                            className="p-2 rounded-lg text-slate-500 hover:text-red-400 hover:bg-red-500/10"
                            disabled={batchRows.length === 1}
                            title="Remove row"
                          >
                            <TrashIcon className="h-4 w-4" />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="flex justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowBatchAdd(false)}
                  className="btn-secondary"
                  disabled={batchSubmitting}
                >
                  Cancel
                </button>
                <button type="submit" className="btn-primary" disabled={batchSubmitting}>
                  {batchSubmitting ? 'Creating...' : 'Create Parts & Add to BOM'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* New Part Modal */}
      {showNewPart && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-[60]" onClick={() => setShowNewPart(false)}>
          <div className="bg-[#151b28] rounded-xl p-6 max-w-md w-full mx-4 shadow-xl animate-scale-in" onClick={e => e.stopPropagation()}>
            <h3 className="text-lg font-semibold text-white mb-4">Create New Part</h3>
            <form onSubmit={handleCreateNewPart} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Part Number *</label>
                  <input
                    type="text"
                    value={newPart.part_number}
                    onChange={e => setNewPart(prev => ({ ...prev, part_number: e.target.value.toUpperCase() }))}
                    className="input"
                    required
                    autoFocus
                  />
                </div>
                <div>
                  <label className="label">Revision</label>
                  <input
                    type="text"
                    value={newPart.revision}
                    onChange={e => setNewPart(prev => ({ ...prev, revision: e.target.value.toUpperCase() }))}
                    className="input"
                    required
                  />
                </div>
              </div>

              <div>
                <label className="label">Name *</label>
                <input
                  type="text"
                  value={newPart.name}
                  onChange={e => setNewPart(prev => ({ ...prev, name: e.target.value }))}
                  className="input"
                  required
                />
              </div>

              <div>
                <label className="label">Type *</label>
                <select
                  value={newPart.part_type}
                  onChange={e => setNewPart(prev => ({ ...prev, part_type: e.target.value as PartType }))}
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
              </div>

              <div>
                <label className="label">Description</label>
                <textarea
                  value={newPart.description}
                  onChange={e => setNewPart(prev => ({ ...prev, description: e.target.value }))}
                  className="input"
                  rows={2}
                />
              </div>

              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={() => setShowNewPart(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary">
                  Create & Select
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Import Wizard */}
      {showImport && (
        <BOMImportWizard
          onComplete={async () => {
            await onBOMChanged();
            setShowImport(false);
          }}
          onClose={() => setShowImport(false)}
        />
      )}

      {/* Confirm Dialog */}
      <ConfirmDialog
        open={!!confirmAction}
        title={confirmAction?.title || ''}
        message={confirmAction?.message || ''}
        confirmLabel="Delete"
        onConfirm={() => {
          confirmAction?.action();
          setConfirmAction(null);
        }}
        onCancel={() => setConfirmAction(null)}
      />
    </div>
  );
}

// ── Row Components ─────────────────────────────────────────────────────────

function SingleLevelRow({ item, isDraft, onDelete, onNavigate }: {
  item: BOMItem;
  isDraft: boolean;
  onDelete: (id: number) => void;
  onNavigate: (partId: number) => void;
}) {
  return (
    <tr className={`hover:bg-slate-700/50 ${item.line_type === 'hardware' ? 'bg-amber-500/5' : ''}`}>
      <td className="px-4 py-3 text-sm font-medium">{item.find_number || item.item_number}</td>
      <td className="px-4 py-3">
        <div className="flex items-center gap-2">
          <div>
            <button
              onClick={() => item.component_part?.id && onNavigate(item.component_part.id)}
              className="font-medium text-werco-navy-600 hover:text-werco-navy-800 hover:underline text-sm"
            >
              {item.component_part?.part_number}
            </button>
            <div className="text-xs text-slate-400">{item.component_part?.name}</div>
            {item.torque_spec && <div className="text-xs text-amber-600">Torque: {item.torque_spec}</div>}
            {item.installation_notes && <div className="text-xs text-slate-500 italic">{item.installation_notes}</div>}
          </div>
          {item.component_part?.has_bom && (
            <DocumentDuplicateIcon className="h-4 w-4 text-blue-400 flex-shrink-0" title="Has BOM" />
          )}
        </div>
      </td>
      <td className="px-4 py-3">
        <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${lineTypeColors[item.line_type || 'component']}`}>
          {lineTypeLabels[item.line_type || 'component']}
        </span>
      </td>
      <td className="px-4 py-3 text-right text-sm">{item.quantity}</td>
      <td className="px-4 py-3 text-center text-sm text-slate-400">{item.unit_of_measure}</td>
      {isDraft && (
        <td className="px-4 py-3 text-center">
          <button onClick={() => onDelete(item.id)} className="text-slate-500 hover:text-red-400 p-1">
            <TrashIcon className="h-4 w-4" />
          </button>
        </td>
      )}
    </tr>
  );
}

function ExplodedRow({ item, depth, expandedItems, onToggle }: {
  item: BOMItem;
  depth: number;
  expandedItems: Set<number>;
  onToggle: (id: number) => void;
}) {
  const hasChildren = item.children && item.children.length > 0;
  const isExpanded = expandedItems.has(item.id);

  return (
    <>
      <tr className="hover:bg-slate-700/50">
        <td className="px-4 py-3" style={{ paddingLeft: `${16 + depth * 24}px` }}>
          <div className="flex items-center">
            {hasChildren ? (
              <button onClick={() => onToggle(item.id)} className="mr-2">
                {isExpanded
                  ? <ChevronDownIcon className="h-4 w-4 text-slate-400" />
                  : <ChevronRightIcon className="h-4 w-4 text-slate-400" />
                }
              </button>
            ) : <span className="w-6" />}
            <span className="text-xs text-slate-500">L{item.level || 0}</span>
          </div>
        </td>
        <td className="px-4 py-3 text-sm font-medium">{item.find_number || item.item_number}</td>
        <td className="px-4 py-3">
          <div className="font-medium text-sm text-werco-navy-600">{item.component_part?.part_number}</div>
          <div className="text-xs text-slate-400">{item.component_part?.name}</div>
        </td>
        <td className="px-4 py-3">
          <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${lineTypeColors[item.line_type || 'component']}`}>
            {lineTypeLabels[item.line_type || 'component']}
          </span>
        </td>
        <td className="px-4 py-3 text-right text-sm">{item.quantity}</td>
        <td className="px-4 py-3 text-right text-sm">{item.extended_quantity?.toFixed(3) || item.quantity}</td>
        <td className="px-4 py-3 text-center text-sm text-slate-400">{item.unit_of_measure}</td>
      </tr>
      {hasChildren && isExpanded && item.children!.map(child => (
        <ExplodedRow
          key={`${child.id}-${(child.level || 0)}`}
          item={child}
          depth={depth + 1}
          expandedItems={expandedItems}
          onToggle={onToggle}
        />
      ))}
    </>
  );
}
