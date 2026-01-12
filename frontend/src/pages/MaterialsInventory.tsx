import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../services/api';
import {
  ArrowsRightLeftIcon,
  ArrowDownTrayIcon,
  XMarkIcon,
  ExclamationTriangleIcon,
  WrenchScrewdriverIcon,
  CubeIcon,
  PlusIcon,
} from '@heroicons/react/24/outline';

interface InventoryItem {
  id: number;
  part_id: number;
  part?: { id: number; part_number: string; name: string; part_type: string };
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
  part_type?: string;
  total_on_hand: number;
  total_allocated: number;
  available: number;
  locations: Array<{ location: string; quantity: number; lot_number?: string }>;
}

type TabType = 'summary' | 'details';

export default function MaterialsInventoryPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<TabType>('summary');
  const [inventory, setInventory] = useState<InventoryItem[]>([]);
  const [summary, setSummary] = useState<InventorySummary[]>([]);
  const [lowStockItems, setLowStockItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [parts, setParts] = useState<any[]>([]);
  const [locations, setLocations] = useState<any[]>([]);
  const [showLowStockOnly, setShowLowStockOnly] = useState(() => searchParams.get('filter') === 'low_stock');
  
  const [showReceiveModal, setShowReceiveModal] = useState(false);
  const [showTransferModal, setShowTransferModal] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedItem, setSelectedItem] = useState<InventoryItem | null>(null);

  const [receiveForm, setReceiveForm] = useState({
    part_id: 0, quantity: 0, location_code: '', lot_number: '', 
    serial_number: '', po_number: '', unit_cost: 0
  });
  const [transferForm, setTransferForm] = useState({
    inventory_item_id: 0, quantity: 0, to_location_code: '', notes: ''
  });
  const [createForm, setCreateForm] = useState({
    part_number: '',
    name: '',
    description: '',
    part_type: 'raw_material' as 'raw_material' | 'purchased',
    unit_of_measure: 'each',
    standard_cost: 0,
  });

  // Filter for raw materials and purchased parts only
  const materialTypes = ['raw_material', 'purchased'];

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      const [invRes, summaryRes, partsRes, locsRes, lowStockRes] = await Promise.all([
        api.getInventory(),
        api.getInventorySummary(),
        api.getParts({ active_only: true }),
        api.getInventoryLocations(),
        api.getLowStockAlerts()
      ]);
      
      // Filter for materials only (raw_material and purchased)
      const materialParts = partsRes.filter((p: any) => materialTypes.includes(p.part_type));
      const materialPartIds = new Set(materialParts.map((p: any) => p.id));
      
      setInventory(invRes.filter((i: InventoryItem) => materialPartIds.has(i.part_id)));
      setSummary(summaryRes.filter((s: InventorySummary) => {
        const part = partsRes.find((p: any) => p.id === s.part_id);
        return part && materialTypes.includes(part.part_type);
      }));
      setParts(materialParts);
      setLocations(locsRes);
      setLowStockItems(lowStockRes.filter((ls: any) => materialPartIds.has(ls.part_id)));
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

  const handleCreateMaterial = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.createPart({
        part_number: createForm.part_number,
        name: createForm.name,
        description: createForm.description || undefined,
        part_type: createForm.part_type,
        unit_of_measure: createForm.unit_of_measure,
        standard_cost: createForm.standard_cost || 0,
        revision: 'A',
      });
      setShowCreateModal(false);
      setCreateForm({
        part_number: '',
        name: '',
        description: '',
        part_type: 'raw_material',
        unit_of_measure: 'each',
        standard_cost: 0,
      });
      loadData();
    } catch (err: any) {
      const errorMsg = err.response?.data?.detail;
      if (Array.isArray(errorMsg)) {
        alert(errorMsg.map((e: any) => e.msg || e).join('\n'));
      } else {
        alert(errorMsg || 'Failed to create material');
      }
    }
  };

  const openTransfer = (item: InventoryItem) => {
    setSelectedItem(item);
    setTransferForm({ ...transferForm, inventory_item_id: item.id, quantity: item.quantity_available });
    setShowTransferModal(true);
  };

  const getPartTypeLabel = (type: string) => {
    switch (type) {
      case 'raw_material': return 'Raw Material';
      case 'purchased': return 'Hardware/Purchased';
      default: return type;
    }
  };

  const getPartTypeIcon = (type: string) => {
    switch (type) {
      case 'raw_material': return <CubeIcon className="h-4 w-4" />;
      case 'purchased': return <WrenchScrewdriverIcon className="h-4 w-4" />;
      default: return null;
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-cyan-500"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Materials & Hardware Inventory</h1>
          <p className="text-sm text-gray-500 mt-1">Raw materials, hardware, and purchased items</p>
        </div>
        <div className="flex gap-3">
          <button onClick={() => setShowCreateModal(true)} className="btn-secondary flex items-center">
            <PlusIcon className="h-5 w-5 mr-2" /> New Material
          </button>
          <button onClick={() => setShowReceiveModal(true)} className="btn-primary flex items-center">
            <ArrowDownTrayIcon className="h-5 w-5 mr-2" /> Receive Materials
          </button>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="card">
          <div className="text-2xl font-bold text-cyan-600">{summary.length}</div>
          <div className="text-sm text-gray-500">Unique Materials</div>
        </div>
        <div className="card">
          <div className="text-2xl font-bold">{summary.reduce((a, b) => a + b.total_on_hand, 0).toFixed(0)}</div>
          <div className="text-sm text-gray-500">Total On Hand</div>
        </div>
        <div className="card">
          <div className="text-2xl font-bold text-green-600">{summary.reduce((a, b) => a + b.available, 0).toFixed(0)}</div>
          <div className="text-sm text-gray-500">Total Available</div>
        </div>
        <div className="card">
          <div className="text-2xl font-bold text-amber-600">{lowStockItems.length}</div>
          <div className="text-sm text-gray-500">Low Stock Alerts</div>
        </div>
      </div>

      {/* Low Stock Alert Banner */}
      {showLowStockOnly && (
        <div className="flex items-center justify-between p-4 bg-amber-50 border border-amber-200 rounded-xl">
          <div className="flex items-center gap-3">
            <ExclamationTriangleIcon className="h-5 w-5 text-amber-600" />
            <span className="font-medium text-amber-800">
              Showing {lowStockItems.length} low stock item(s)
            </span>
          </div>
          <button
            onClick={() => {
              setShowLowStockOnly(false);
              setSearchParams({});
            }}
            className="flex items-center gap-1 px-3 py-1.5 text-sm bg-amber-100 text-amber-700 rounded-full hover:bg-amber-200"
          >
            <XMarkIcon className="h-4 w-4" />
            Clear filter
          </button>
        </div>
      )}

      {/* Tabs */}
      <div className="border-b border-gray-200">
        <nav className="-mb-px flex space-x-8">
          {[
            { id: 'summary', label: 'Summary by Material' },
            { id: 'details', label: 'Detail by Location' },
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id as TabType)}
              className={`py-4 px-1 border-b-2 font-medium text-sm ${
                activeTab === tab.id
                  ? 'border-cyan-500 text-cyan-600'
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
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Material</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">On Hand</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Allocated</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Available</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Locations</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {(showLowStockOnly 
                  ? summary.filter(item => lowStockItems.some(ls => ls.part_id === item.part_id))
                  : summary
                ).map((item) => {
                  const isLowStock = lowStockItems.some(ls => ls.part_id === item.part_id);
                  const part = parts.find(p => p.id === item.part_id);
                  return (
                    <tr key={item.part_id} className={`hover:bg-gray-50 align-top ${isLowStock ? 'bg-red-50' : ''}`}>
                      <td className="px-4 py-3">
                        <div className="font-medium">{item.part_number}</div>
                        <div className="text-sm text-gray-500">{item.part_name}</div>
                        {isLowStock && <span className="text-xs text-red-600 font-medium">LOW STOCK</span>}
                      </td>
                      <td className="px-4 py-3">
                        {part && (
                          <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium ${
                            part.part_type === 'raw_material' 
                              ? 'bg-blue-100 text-blue-700' 
                              : 'bg-purple-100 text-purple-700'
                          }`}>
                            {getPartTypeIcon(part.part_type)}
                            {getPartTypeLabel(part.part_type)}
                          </span>
                        )}
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
            {summary.length === 0 && <p className="text-center text-gray-500 py-8">No materials inventory on hand</p>}
          </div>
        )}

        {activeTab === 'details' && (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Material</th>
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
                {inventory.map((item) => (
                  <tr key={item.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3">
                      <div className="font-medium">{item.part?.part_number}</div>
                      <div className="text-xs text-gray-500">{item.part?.name}</div>
                    </td>
                    <td className="px-4 py-3">
                      {item.part && (
                        <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium ${
                          item.part.part_type === 'raw_material' 
                            ? 'bg-blue-100 text-blue-700' 
                            : 'bg-purple-100 text-purple-700'
                        }`}>
                          {getPartTypeLabel(item.part.part_type)}
                        </span>
                      )}
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
                      <button onClick={() => openTransfer(item)} className="text-cyan-600 hover:text-cyan-700">
                        <ArrowsRightLeftIcon className="h-5 w-5" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {inventory.length === 0 && <p className="text-center text-gray-500 py-8">No materials inventory on hand</p>}
          </div>
        )}
      </div>

      {/* Receive Modal */}
      {showReceiveModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4">
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold">Receive Materials</h3>
              <button onClick={() => setShowReceiveModal(false)}><XMarkIcon className="h-6 w-6" /></button>
            </div>
            <form onSubmit={handleReceive} className="space-y-4">
              <div>
                <label className="label">Material</label>
                <select value={receiveForm.part_id} onChange={(e) => setReceiveForm({...receiveForm, part_id: parseInt(e.target.value)})} className="input" required>
                  <option value={0}>Select material...</option>
                  {parts.map(p => <option key={p.id} value={p.id}>{p.part_number} - {p.name}</option>)}
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
              <h3 className="text-lg font-semibold">Transfer Material</h3>
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

      {/* Create Material Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold">Create New Material</h3>
              <button onClick={() => setShowCreateModal(false)}><XMarkIcon className="h-6 w-6" /></button>
            </div>
            <form onSubmit={handleCreateMaterial} className="space-y-4">
              <div>
                <label className="label">Material Type *</label>
                <select 
                  value={createForm.part_type} 
                  onChange={(e) => setCreateForm({...createForm, part_type: e.target.value as 'raw_material' | 'purchased'})} 
                  className="input" 
                  required
                >
                  <option value="raw_material">Raw Material (steel, aluminum, etc.)</option>
                  <option value="purchased">Hardware/Purchased (bolts, screws, etc.)</option>
                </select>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Part Number *</label>
                  <input 
                    type="text" 
                    value={createForm.part_number} 
                    onChange={(e) => {
                      // Only allow uppercase letters, numbers, and hyphens
                      const cleaned = e.target.value.toUpperCase().replace(/[^A-Z0-9\-]/g, '');
                      setCreateForm({...createForm, part_number: cleaned});
                    }} 
                    className="input" 
                    placeholder="e.g., STEEL-1018-05"
                    required 
                  />
                </div>
                <div>
                  <label className="label">Unit of Measure *</label>
                  <select 
                    value={createForm.unit_of_measure} 
                    onChange={(e) => setCreateForm({...createForm, unit_of_measure: e.target.value})} 
                    className="input" 
                    required
                  >
                    <option value="each">Each</option>
                    <option value="feet">Feet</option>
                    <option value="inches">Inches</option>
                    <option value="pounds">Pounds</option>
                    <option value="kilograms">Kilograms</option>
                    <option value="sheets">Sheets</option>
                    <option value="gallons">Gallons</option>
                    <option value="liters">Liters</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="label">Name *</label>
                <input 
                  type="text" 
                  value={createForm.name} 
                  onChange={(e) => setCreateForm({...createForm, name: e.target.value})} 
                  className="input" 
                  placeholder="e.g., 1018 Cold Rolled Steel 0.5in"
                  required 
                />
              </div>
              <div>
                <label className="label">Description</label>
                <textarea 
                  value={createForm.description} 
                  onChange={(e) => setCreateForm({...createForm, description: e.target.value})} 
                  className="input" 
                  rows={2}
                  placeholder="Optional detailed description"
                />
              </div>
              <div>
                <label className="label">Standard Cost ($)</label>
                <input 
                  type="number" 
                  value={createForm.standard_cost} 
                  onChange={(e) => setCreateForm({...createForm, standard_cost: parseFloat(e.target.value) || 0})} 
                  className="input" 
                  min={0} 
                  step={0.01}
                  placeholder="0.00"
                />
              </div>
              <div className="flex justify-end gap-3 pt-4 border-t">
                <button type="button" onClick={() => setShowCreateModal(false)} className="btn-secondary">Cancel</button>
                <button type="submit" className="btn-primary">Create Material</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
