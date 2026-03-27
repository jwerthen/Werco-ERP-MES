import React, { useEffect, useState, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../../services/api';
import { Part } from '../../types';
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
    if (!search) return allParts.slice(0, 50);
    return allParts
      .filter(p =>
        p.part_number.toLowerCase().includes(search) ||
        p.name.toLowerCase().includes(search)
      )
      .slice(0, 50);
  }, [allParts, partSearch]);

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
        <DocumentDuplicateIcon className="h-12 w-12 mx-auto text-gray-300 mb-4" />
        <h3 className="text-lg font-medium text-gray-900 mb-2">No Bill of Materials</h3>
        <p className="text-sm text-gray-500 mb-6 max-w-md mx-auto">
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
          <span className="text-sm text-gray-500">Rev {bom.revision}</span>
          <span className="text-sm text-gray-500">{bom.items.length} item{bom.items.length !== 1 ? 's' : ''}</span>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {/* View Toggle */}
          <div className="flex rounded-lg border border-gray-300 overflow-hidden">
            <button
              onClick={() => setViewMode('single')}
              className={`px-3 py-1.5 text-xs font-medium ${viewMode === 'single' ? 'bg-werco-navy-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}
            >
              Single Level
            </button>
            <button
              onClick={() => setViewMode('exploded')}
              className={`px-3 py-1.5 text-xs font-medium ${viewMode === 'exploded' ? 'bg-werco-navy-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}
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
                {viewMode === 'single' && bom.status === 'draft' && (
                  <th className="px-4 py-3 w-16" />
                )}
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
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
                    <td colSpan={7} className="py-12 text-center text-gray-500">
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
                    <td colSpan={7} className="py-12 text-center text-gray-500">
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
          <div className="bg-white rounded-xl p-6 max-w-lg w-full mx-4 shadow-xl animate-scale-in" onClick={e => e.stopPropagation()}>
            <h3 className="text-lg font-semibold mb-4">Add BOM Item</h3>
            <form onSubmit={handleAddItem} className="space-y-4">
              {/* Part Search */}
              <div>
                <label className="label">Component Part</label>
                <div className="relative">
                  <MagnifyingGlassIcon className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
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
                  <div className="mt-1 max-h-40 overflow-y-auto border border-gray-200 rounded-lg">
                    {filteredParts.map(p => (
                      <button
                        key={p.id}
                        type="button"
                        onClick={() => {
                          setNewItem(prev => ({ ...prev, component_part_id: p.id }));
                          setPartSearch(`${p.part_number} - ${p.name}`);
                        }}
                        className={`w-full text-left px-3 py-2 text-sm hover:bg-gray-50 ${
                          p.id === newItem.component_part_id ? 'bg-blue-50' : ''
                        }`}
                      >
                        <span className="font-medium">{p.part_number}</span>
                        <span className="text-gray-500 ml-2">{p.name}</span>
                      </button>
                    ))}
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
                    step={10}
                  />
                </div>
                <div>
                  <label className="label">Quantity</label>
                  <input
                    type="number"
                    value={newItem.quantity}
                    onChange={e => setNewItem(prev => ({ ...prev, quantity: parseFloat(e.target.value) || 0 }))}
                    className="input"
                    step="0.001"
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
    <tr className={`hover:bg-gray-50 ${item.line_type === 'hardware' ? 'bg-amber-50/30' : ''}`}>
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
            <div className="text-xs text-gray-500">{item.component_part?.name}</div>
            {item.torque_spec && <div className="text-xs text-amber-600">Torque: {item.torque_spec}</div>}
            {item.installation_notes && <div className="text-xs text-gray-400 italic">{item.installation_notes}</div>}
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
      <td className="px-4 py-3 text-center text-sm text-gray-500">{item.unit_of_measure}</td>
      {isDraft && (
        <td className="px-4 py-3 text-center">
          <button onClick={() => onDelete(item.id)} className="text-gray-400 hover:text-red-500 p-1">
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
      <tr className="hover:bg-gray-50">
        <td className="px-4 py-3" style={{ paddingLeft: `${16 + depth * 24}px` }}>
          <div className="flex items-center">
            {hasChildren ? (
              <button onClick={() => onToggle(item.id)} className="mr-2">
                {isExpanded
                  ? <ChevronDownIcon className="h-4 w-4 text-gray-500" />
                  : <ChevronRightIcon className="h-4 w-4 text-gray-500" />
                }
              </button>
            ) : <span className="w-6" />}
            <span className="text-xs text-gray-400">L{item.level || 0}</span>
          </div>
        </td>
        <td className="px-4 py-3 text-sm font-medium">{item.find_number || item.item_number}</td>
        <td className="px-4 py-3">
          <div className="font-medium text-sm text-werco-navy-600">{item.component_part?.part_number}</div>
          <div className="text-xs text-gray-500">{item.component_part?.name}</div>
        </td>
        <td className="px-4 py-3">
          <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${lineTypeColors[item.line_type || 'component']}`}>
            {lineTypeLabels[item.line_type || 'component']}
          </span>
        </td>
        <td className="px-4 py-3 text-right text-sm">{item.quantity}</td>
        <td className="px-4 py-3 text-right text-sm">{item.extended_quantity?.toFixed(3) || item.quantity}</td>
        <td className="px-4 py-3 text-center text-sm text-gray-500">{item.unit_of_measure}</td>
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
