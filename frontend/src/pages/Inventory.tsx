import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../services/api';
import {
  ArrowsRightLeftIcon,
  ArrowDownTrayIcon,
  XMarkIcon,
  ExclamationTriangleIcon,
  CubeIcon,
  Squares2X2Icon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline';

interface InventoryItem {
  id: number;
  part_id: number;
  part?: { id: number; part_number: string; name: string; part_type?: string };
  location: string;
  warehouse: string;
  quantity_on_hand: number;
  quantity_allocated: number;
  quantity_available: number;
  lot_number?: string;
  serial_number?: string;
  status: string;
  unit_cost: number;
}

interface InventorySummary {
  part_id: number;
  part_number: string;
  part_name: string;
  total_on_hand: number;
  total_allocated: number;
  available: number;
  locations: Array<{ location: string; quantity: number; lot_number?: string }>;
}

type TabType = 'summary' | 'details' | 'receive' | 'transactions';
type InventoryGroup = 'all' | 'parts' | 'materials';

const MATERIAL_TYPES = new Set(['raw_material', 'purchased', 'hardware', 'consumable']);
const PART_TYPES = new Set(['manufactured', 'assembly']);

export default function InventoryPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<TabType>('summary');
  const [groupFilter, setGroupFilter] = useState<InventoryGroup>(() => {
    const group = searchParams.get('group');
    return group === 'parts' || group === 'materials' ? group : 'all';
  });
  const [inventory, setInventory] = useState<InventoryItem[]>([]);
  const [summary, setSummary] = useState<InventorySummary[]>([]);
  const [lowStockItems, setLowStockItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [parts, setParts] = useState<any[]>([]);
  const [locations, setLocations] = useState<any[]>([]);
  const [showLowStockOnly, setShowLowStockOnly] = useState(() => searchParams.get('filter') === 'low_stock');
  const [filterText, setFilterText] = useState('');
  
  const [showReceiveModal, setShowReceiveModal] = useState(false);
  const [showTransferModal, setShowTransferModal] = useState(false);
  const [selectedItem, setSelectedItem] = useState<InventoryItem | null>(null);

  const [receiveForm, setReceiveForm] = useState({
    part_id: 0, quantity: 0, location_code: '', lot_number: '', 
    serial_number: '', po_number: '', unit_cost: 0
  });
  const [transferForm, setTransferForm] = useState({
    inventory_item_id: 0, quantity: 0, to_location_code: '', notes: ''
  });
  const lowStockPartIds = useMemo(
    () => new Set(lowStockItems.map((item: any) => item.part_id)),
    [lowStockItems]
  );
  const partsById = useMemo(() => new Map(parts.map((p: any) => [p.id, p])), [parts]);
  const getPartType = useCallback((partId: number) => {
    return partsById.get(partId)?.part_type as string | undefined;
  }, [partsById]);
  const filterByGroup = useCallback((partType?: string) => {
    if (!partType) return groupFilter === 'all';
    if (groupFilter === 'parts') return PART_TYPES.has(partType);
    if (groupFilter === 'materials') return MATERIAL_TYPES.has(partType);
    return true;
  }, [groupFilter]);
  const filteredSummary = useMemo(() => {
    const base = showLowStockOnly
      ? summary.filter((item) => lowStockPartIds.has(item.part_id))
      : summary;
    const grouped = base.filter((item) => filterByGroup(getPartType(item.part_id)));
    if (!filterText) return grouped;
    const term = filterText.toLowerCase();
    return grouped.filter((item) => (
      item.part_number?.toLowerCase().includes(term) ||
      item.part_name?.toLowerCase().includes(term)
    ));
  }, [filterText, filterByGroup, getPartType, lowStockPartIds, showLowStockOnly, summary]);
  const groupSummary = useMemo(
    () => summary.filter((item) => filterByGroup(getPartType(item.part_id))),
    [filterByGroup, getPartType, summary]
  );
  const filteredInventory = useMemo(() => {
    const grouped = inventory.filter((item) => filterByGroup(item.part?.part_type));
    if (!filterText) return grouped;
    const term = filterText.toLowerCase();
    return grouped.filter((item) => (
      item.part?.part_number?.toLowerCase().includes(term) ||
      item.part?.name?.toLowerCase().includes(term)
    ));
  }, [filterByGroup, filterText, inventory]);
  const groupInventory = useMemo(
    () => inventory.filter((item) => filterByGroup(item.part?.part_type)),
    [filterByGroup, inventory]
  );
  const filteredPartsForReceive = useMemo(() => {
    return parts.filter((part: any) => filterByGroup(part.part_type));
  }, [filterByGroup, parts]);
  const summaryTotals = useMemo(() => {
    return filteredSummary.reduce(
      (acc, item) => {
        acc.totalOnHand += item.total_on_hand;
        acc.totalAvailable += item.available;
        return acc;
      },
      { totalOnHand: 0, totalAvailable: 0 }
    );
  }, [filteredSummary]);
  const lowStockCount = useMemo(() => {
    return lowStockItems.filter((item: any) => filterByGroup(getPartType(item.part_id))).length;
  }, [filterByGroup, getPartType, lowStockItems]);

  useEffect(() => {
    const group = searchParams.get('group');
    if (group === 'parts' || group === 'materials') {
      setGroupFilter(group);
    } else if (group === null) {
      setGroupFilter('all');
    }
  }, [searchParams]);

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    setLoading(true);
    try {
      const [invRes, summaryRes, partsRes, locsRes, lowStockRes] = await Promise.all([
        api.getInventory(),
        api.getInventorySummary(),
        api.getParts({ active_only: true }),
        api.getInventoryLocations(),
        api.getLowStockAlerts()
      ]);
      setInventory(invRes);
      setSummary(summaryRes);
      setParts(partsRes);
      setLocations(locsRes);
      setLowStockItems(lowStockRes);
    } catch (err) {
      console.error('Failed to load inventory:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleReceive = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.receiveInventory(receiveForm);
      setShowReceiveModal(false);
      setReceiveForm({ part_id: 0, quantity: 0, location_code: '', lot_number: '', serial_number: '', po_number: '', unit_cost: 0 });
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to receive inventory');
    }
  };

  const handleTransfer = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.transferInventory(transferForm);
      setShowTransferModal(false);
      setTransferForm({ inventory_item_id: 0, quantity: 0, to_location_code: '', notes: '' });
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to transfer inventory');
    }
  };

  const openTransfer = (item: InventoryItem) => {
    setSelectedItem(item);
    setTransferForm({ ...transferForm, inventory_item_id: item.id, quantity: item.quantity_available });
    setShowTransferModal(true);
  };

  const getPartTypeLabel = (type?: string) => {
    switch (type) {
      case 'manufactured': return 'Manufactured';
      case 'assembly': return 'Assembly';
      case 'raw_material': return 'Raw Material';
      case 'purchased': return 'Purchased';
      case 'hardware': return 'Hardware';
      case 'consumable': return 'Consumable';
      default: return type || '—';
    }
  };

  const getPartTypeIcon = (type?: string) => {
    switch (type) {
      case 'manufactured': return <CubeIcon className="h-4 w-4" />;
      case 'assembly': return <Squares2X2Icon className="h-4 w-4" />;
      case 'raw_material': return <CubeIcon className="h-4 w-4" />;
      case 'purchased': return <WrenchScrewdriverIcon className="h-4 w-4" />;
      case 'hardware': return <WrenchScrewdriverIcon className="h-4 w-4" />;
      case 'consumable': return <CubeIcon className="h-4 w-4" />;
      default: return null;
    }
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
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Inventory</h1>
          <p className="text-sm text-gray-500 mt-1">Parts, materials, hardware, and assemblies in one place</p>
        </div>
        <button onClick={() => setShowReceiveModal(true)} className="btn-primary flex items-center">
          <ArrowDownTrayIcon className="h-5 w-5 mr-2" /> Receive Inventory
        </button>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="card">
          <div className="text-2xl font-bold">{filteredSummary.length}</div>
          <div className="text-sm text-gray-500">Unique Items</div>
        </div>
        <div className="card">
          <div className="text-2xl font-bold">{summaryTotals.totalOnHand.toFixed(0)}</div>
          <div className="text-sm text-gray-500">Total On Hand</div>
        </div>
        <div className="card">
          <div className="text-2xl font-bold">{summaryTotals.totalAvailable.toFixed(0)}</div>
          <div className="text-sm text-gray-500">Total Available</div>
        </div>
        <div className="card">
          <div className="text-2xl font-bold text-amber-600">{lowStockCount}</div>
          <div className="text-sm text-gray-500">Low Stock Alerts</div>
        </div>
      </div>

      {/* Low Stock Alert Banner */}
      {showLowStockOnly && (
        <div className="flex items-center justify-between p-4 bg-amber-50 border border-amber-200 rounded-xl">
          <div className="flex items-center gap-3">
            <ExclamationTriangleIcon className="h-5 w-5 text-amber-600" />
            <span className="font-medium text-amber-800">
              Showing {lowStockCount} low stock item(s)
            </span>
          </div>
          <button
            onClick={() => {
              setShowLowStockOnly(false);
              const nextParams = new URLSearchParams(searchParams);
              nextParams.delete('filter');
              setSearchParams(nextParams);
            }}
            className="flex items-center gap-1 px-3 py-1.5 text-sm bg-amber-100 text-amber-700 rounded-full hover:bg-amber-200"
          >
            <XMarkIcon className="h-4 w-4" />
            Clear filter
          </button>
        </div>
      )}

      {/* Quick Filters */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-col gap-3 w-full sm:max-w-xl">
          <div className="relative">
            <input
              type="text"
              value={filterText}
              onChange={(e) => setFilterText(e.target.value)}
              placeholder="Filter by part number or name..."
              className="input pr-10"
            />
            {filterText && (
              <button
                type="button"
                onClick={() => setFilterText('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                aria-label="Clear filter"
              >
                <XMarkIcon className="h-4 w-4" />
              </button>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {[
              { id: 'all', label: 'All Inventory' },
              { id: 'parts', label: 'Manufactured & Assemblies' },
              { id: 'materials', label: 'Materials & Hardware' },
            ].map((chip) => (
              <button
                key={chip.id}
                type="button"
                onClick={() => {
                  const next = chip.id as InventoryGroup;
                  setGroupFilter(next);
                  const nextParams = new URLSearchParams(searchParams);
                  if (next === 'all') {
                    nextParams.delete('group');
                  } else {
                    nextParams.set('group', next);
                  }
                  setSearchParams(nextParams);
                }}
                className={`rounded-full border px-3 py-1 font-medium transition ${
                  groupFilter === chip.id
                    ? 'border-werco-500 bg-werco-50 text-werco-700'
                    : 'border-gray-200 text-gray-600 hover:border-werco-300'
                }`}
              >
                {chip.label}
              </button>
            ))}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm text-gray-600">
          <span>Showing</span>
          <span className="px-2 py-1 rounded-full bg-gray-100 text-gray-700 font-medium">
            {activeTab === 'details' ? filteredInventory.length : filteredSummary.length}
          </span>
          <span>of</span>
          <span className="px-2 py-1 rounded-full bg-gray-100 text-gray-700 font-medium">
            {activeTab === 'details' ? groupInventory.length : groupSummary.length}
          </span>
          <span>items</span>
          <button
            type="button"
            onClick={() => {
              const next = !showLowStockOnly;
              setShowLowStockOnly(next);
              const nextParams = new URLSearchParams(searchParams);
              if (next) {
                nextParams.set('filter', 'low_stock');
              } else {
                nextParams.delete('filter');
              }
              setSearchParams(nextParams);
            }}
            className={`ml-2 px-3 py-1 rounded-full text-xs font-medium border ${
              showLowStockOnly
                ? 'bg-amber-100 text-amber-700 border-amber-200'
                : 'bg-white text-gray-600 border-gray-200 hover:bg-gray-50'
            }`}
          >
            {showLowStockOnly ? 'Showing Low Stock' : 'Show Low Stock'}
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200">
        <nav className="-mb-px flex space-x-8">
          {[
            { id: 'summary', label: 'Summary by Part' },
            { id: 'details', label: 'Detail by Location' },
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id as TabType)}
              className={`py-4 px-1 border-b-2 font-medium text-sm ${
                activeTab === tab.id
                  ? 'border-werco-primary text-werco-primary'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Content */}
      <div className="card">
        {activeTab === 'summary' && (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">On Hand</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Allocated</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Available</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Locations</th>
                </tr>
              </thead>
                <tbody className="bg-white divide-y divide-gray-200">
                {filteredSummary.map((item) => {
                  const isLowStock = lowStockPartIds.has(item.part_id);
                  const partType = getPartType(item.part_id);
                  return (
                    <tr key={item.part_id} className={`hover:bg-gray-50 align-top ${isLowStock ? 'bg-red-50' : ''}`}>
                      <td className="px-4 py-3">
                        <div className="font-medium">{item.part_number}</div>
                        <div className="text-sm text-gray-500">{item.part_name}</div>
                        {isLowStock && <span className="text-xs text-red-600 font-medium">LOW STOCK</span>}
                      </td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium ${
                          PART_TYPES.has(partType || '')
                            ? 'bg-cyan-100 text-cyan-700'
                            : 'bg-amber-100 text-amber-700'
                        }`}>
                          {getPartTypeIcon(partType)}
                          {getPartTypeLabel(partType)}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right font-medium">{item.total_on_hand}</td>
                      <td className="px-4 py-3 text-right">{item.total_allocated}</td>
                      <td className="px-4 py-3 text-right text-green-600 font-medium">{item.available}</td>
                      <td className="px-4 py-3">
                        {item.locations.map((loc, idx) => (
                          <div key={idx} className="text-sm">
                            <span className="font-mono bg-gray-100 px-1 rounded">{loc.location}</span>
                            <span className="text-gray-600 ml-2">({loc.quantity})</span>
                            {loc.lot_number && <span className="text-gray-400 ml-1">Lot: {loc.lot_number}</span>}
                          </div>
                        ))}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {summary.length === 0 && <p className="text-center text-gray-500 py-8">No inventory on hand</p>}
          </div>
        )}

        {activeTab === 'details' && (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Location</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Lot #</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Available</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {filteredInventory.map((item) => (
                  <tr key={item.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3">
                      <div className="font-medium">{item.part?.part_number}</div>
                      <div className="text-xs text-gray-500">{item.part?.name}</div>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium ${
                        PART_TYPES.has(item.part?.part_type || '')
                          ? 'bg-cyan-100 text-cyan-700'
                          : 'bg-amber-100 text-amber-700'
                      }`}>
                        {getPartTypeIcon(item.part?.part_type)}
                        {getPartTypeLabel(item.part?.part_type)}
                      </span>
                    </td>
                    <td className="px-4 py-3 font-mono text-sm">{item.location}</td>
                    <td className="px-4 py-3 text-sm">{item.lot_number || '-'}</td>
                    <td className="px-4 py-3 text-right font-medium">{item.quantity_on_hand}</td>
                    <td className="px-4 py-3 text-right text-green-600">{item.quantity_available}</td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-1 rounded text-xs ${
                        item.status === 'available' ? 'bg-green-100 text-green-800' :
                        item.status === 'quarantine' ? 'bg-yellow-100 text-yellow-800' :
                        'bg-gray-100 text-gray-800'
                      }`}>{item.status}</span>
                    </td>
                    <td className="px-4 py-3 text-center">
                      <button onClick={() => openTransfer(item)} className="text-werco-primary hover:text-blue-700">
                        <ArrowsRightLeftIcon className="h-5 w-5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {inventory.length === 0 && <p className="text-center text-gray-500 py-8">No inventory on hand</p>}
          </div>
        )}
      </div>

      {/* Receive Modal */}
      {showReceiveModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4">
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold">Receive Inventory</h3>
              <button onClick={() => setShowReceiveModal(false)}><XMarkIcon className="h-6 w-6" /></button>
            </div>
            <form onSubmit={handleReceive} className="space-y-4">
              <div>
                <label className="label">Part</label>
                <select value={receiveForm.part_id} onChange={(e) => setReceiveForm({...receiveForm, part_id: parseInt(e.target.value)})} className="input" required>
                  <option value={0}>Select part...</option>
                  {filteredPartsForReceive.map(p => (
                    <option key={p.id} value={p.id}>{p.part_number} - {p.name}</option>
                  ))}
                </select>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Quantity</label>
                  <input type="number" value={receiveForm.quantity} onChange={(e) => setReceiveForm({...receiveForm, quantity: parseFloat(e.target.value)})} className="input" min={0.01} step={0.01} required />
                </div>
                <div>
                  <label className="label">Location</label>
                  <select value={receiveForm.location_code} onChange={(e) => setReceiveForm({...receiveForm, location_code: e.target.value})} className="input" required>
                    <option value="">Select location...</option>
                    {locations.map(l => <option key={l.id} value={l.code}>{l.code} - {l.name || l.warehouse}</option>)}
                  </select>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Lot Number</label>
                  <input type="text" value={receiveForm.lot_number} onChange={(e) => setReceiveForm({...receiveForm, lot_number: e.target.value})} className="input" />
                </div>
                <div>
                  <label className="label">PO Number</label>
                  <input type="text" value={receiveForm.po_number} onChange={(e) => setReceiveForm({...receiveForm, po_number: e.target.value})} className="input" />
                </div>
              </div>
              <div>
                <label className="label">Unit Cost</label>
                <input type="number" value={receiveForm.unit_cost} onChange={(e) => setReceiveForm({...receiveForm, unit_cost: parseFloat(e.target.value)})} className="input" min={0} step={0.01} />
              </div>
              <div className="flex justify-end gap-3">
                <button type="button" onClick={() => setShowReceiveModal(false)} className="btn-secondary">Cancel</button>
                <button type="submit" className="btn-primary">Receive</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Transfer Modal */}
      {showTransferModal && selectedItem && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold">Transfer Inventory</h3>
              <button onClick={() => setShowTransferModal(false)}><XMarkIcon className="h-6 w-6" /></button>
            </div>
            <div className="mb-4 p-3 bg-gray-50 rounded">
              <div className="font-medium">{selectedItem.part?.part_number}</div>
              <div className="text-sm text-gray-500">From: {selectedItem.location}</div>
              <div className="text-sm text-gray-500">Available: {selectedItem.quantity_available}</div>
            </div>
            <form onSubmit={handleTransfer} className="space-y-4">
              <div>
                <label className="label">Quantity to Transfer</label>
                <input type="number" value={transferForm.quantity} onChange={(e) => setTransferForm({...transferForm, quantity: parseFloat(e.target.value)})} className="input" min={0.01} max={selectedItem.quantity_available} step={0.01} required />
              </div>
              <div>
                <label className="label">To Location</label>
                <select value={transferForm.to_location_code} onChange={(e) => setTransferForm({...transferForm, to_location_code: e.target.value})} className="input" required>
                  <option value="">Select destination...</option>
                  {locations.filter(l => l.code !== selectedItem.location).map(l => <option key={l.id} value={l.code}>{l.code}</option>)}
                </select>
              </div>
              <div>
                <label className="label">Notes</label>
                <input type="text" value={transferForm.notes} onChange={(e) => setTransferForm({...transferForm, notes: e.target.value})} className="input" />
              </div>
              <div className="flex justify-end gap-3">
                <button type="button" onClick={() => setShowTransferModal(false)} className="btn-secondary">Cancel</button>
                <button type="submit" className="btn-primary">Transfer</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
