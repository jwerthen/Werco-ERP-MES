import React, { useState, useEffect, useMemo, useCallback } from 'react';
import api from '../services/api';
import {
  PlusIcon,
  MagnifyingGlassIcon,
  ArrowPathIcon,
  XMarkIcon,
  ExclamationTriangleIcon,
  ArrowRightOnRectangleIcon,
  ArrowLeftOnRectangleIcon,
} from '@heroicons/react/24/outline';

interface Tool {
  id: number;
  tool_number: string;
  name: string;
  description?: string;
  tool_type: string;
  status: string;
  location?: string;
  assigned_to?: string;
  assigned_to_name?: string;
  manufacturer?: string;
  model_number?: string;
  serial_number?: string;
  purchase_date?: string;
  purchase_cost?: number;
  max_uses?: number;
  current_uses: number;
  max_life_hours?: number;
  current_life_hours: number;
  last_inspection_date?: string;
  next_inspection_date?: string;
  next_replacement_date?: string;
  notes?: string;
  is_active: boolean;
  created_at: string;
}

interface Dashboard {
  total_tools: number;
  checked_out: number;
  replacement_due: number;
  inspection_due: number;
  by_status: Record<string, number>;
  by_type: Record<string, number>;
}

type Tab = 'all' | 'checked_out' | 'replacement' | 'inspection';

const statusColors: Record<string, { bg: string; text: string }> = {
  available: { bg: 'bg-green-500/20', text: 'text-emerald-300' },
  checked_out: { bg: 'bg-blue-500/20', text: 'text-blue-300' },
  in_use: { bg: 'bg-blue-500/20', text: 'text-blue-300' },
  maintenance: { bg: 'bg-yellow-500/20', text: 'text-yellow-300' },
  needs_repair: { bg: 'bg-red-500/20', text: 'text-red-300' },
  retired: { bg: 'bg-slate-800/50', text: 'text-slate-100' },
  lost: { bg: 'bg-red-500/20', text: 'text-red-300' },
};

export default function ToolManagement() {
  const [tools, setTools] = useState<Tool[]>([]);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [activeTab, setActiveTab] = useState<Tab>('all');
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showCheckoutModal, setShowCheckoutModal] = useState(false);
  const [showCheckinModal, setShowCheckinModal] = useState(false);
  const [selectedTool, setSelectedTool] = useState<Tool | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [toolHistory, setToolHistory] = useState<any[]>([]);

  const [createForm, setCreateForm] = useState({
    tool_number: '', name: '', description: '', tool_type: 'cutting_tool',
    location: '', manufacturer: '', model_number: '', serial_number: '',
    purchase_cost: '', max_uses: '', max_life_hours: '',
  });
  const [checkoutForm, setCheckoutForm] = useState({ checked_out_to: '', work_order_id: '', notes: '' });
  const [checkinForm, setCheckinForm] = useState({ condition: 'good', notes: '', uses_this_session: '' });

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      setError('');
      const [dashData, toolsData] = await Promise.all([
        api.getToolDashboard().catch(() => null),
        api.getTools({ include_inactive: false }),
      ]);
      setDashboard(dashData);
      setTools(toolsData || []);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load tools');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const loadTabData = useCallback(async (tab: Tab) => {
    try {
      let data: Tool[] = [];
      if (tab === 'checked_out') data = await api.getToolsCheckedOut();
      else if (tab === 'replacement') data = await api.getToolsReplacementDue();
      else if (tab === 'inspection') data = await api.getToolsInspectionDue();
      else data = await api.getTools({ include_inactive: false });
      setTools(data || []);
    } catch (err) {
      console.error('Failed to load tab data:', err);
    }
  }, []);

  useEffect(() => { loadTabData(activeTab); }, [activeTab, loadTabData]);

  const filteredTools = useMemo(() => {
    return tools.filter(t => {
      if (statusFilter && t.status !== statusFilter) return false;
      if (typeFilter && t.tool_type !== typeFilter) return false;
      if (search) {
        const s = search.toLowerCase();
        return (
          t.tool_number?.toLowerCase().includes(s) ||
          t.name?.toLowerCase().includes(s) ||
          t.serial_number?.toLowerCase().includes(s) ||
          t.location?.toLowerCase().includes(s)
        );
      }
      return true;
    });
  }, [tools, statusFilter, typeFilter, search]);

  const handleCreate = async () => {
    try {
      await api.createTool({
        ...createForm,
        purchase_cost: createForm.purchase_cost ? parseFloat(createForm.purchase_cost) : undefined,
        max_uses: createForm.max_uses ? parseInt(createForm.max_uses) : undefined,
        max_life_hours: createForm.max_life_hours ? parseFloat(createForm.max_life_hours) : undefined,
      });
      setShowCreateModal(false);
      setCreateForm({ tool_number: '', name: '', description: '', tool_type: 'cutting_tool', location: '', manufacturer: '', model_number: '', serial_number: '', purchase_cost: '', max_uses: '', max_life_hours: '' });
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create tool');
    }
  };

  const handleCheckout = async () => {
    if (!selectedTool) return;
    try {
      await api.checkoutTool(selectedTool.id, {
        ...checkoutForm,
        work_order_id: checkoutForm.work_order_id ? parseInt(checkoutForm.work_order_id) : undefined,
      });
      setShowCheckoutModal(false);
      setCheckoutForm({ checked_out_to: '', work_order_id: '', notes: '' });
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to checkout tool');
    }
  };

  const handleCheckin = async () => {
    if (!selectedTool) return;
    try {
      await api.checkinTool(selectedTool.id, {
        ...checkinForm,
        uses_this_session: checkinForm.uses_this_session ? parseInt(checkinForm.uses_this_session) : undefined,
      });
      setShowCheckinModal(false);
      setCheckinForm({ condition: 'good', notes: '', uses_this_session: '' });
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to checkin tool');
    }
  };

  const toggleExpand = async (id: number) => {
    if (expandedId === id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(id);
    try {
      const history = await api.getToolHistory(id);
      setToolHistory(history || []);
    } catch { setToolHistory([]); }
  };

  if (loading) {
    return <div className="p-6"><div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/4" /><div className="grid grid-cols-4 gap-4">{[...Array(4)].map((_, i) => <div key={i} className="h-24 bg-gray-200 rounded" />)}</div><div className="h-64 bg-gray-200 rounded" /></div></div>;
  }

  if (error) {
    return <div className="p-6"><div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4 flex items-center gap-3"><ExclamationTriangleIcon className="w-5 h-5 text-red-500" /><span className="text-red-400">{error}</span><button onClick={loadData} className="ml-auto text-red-600 hover:text-red-300">Retry</button></div></div>;
  }

  const tabs: { key: Tab; label: string; count?: number }[] = [
    { key: 'all', label: 'All Tools', count: dashboard?.total_tools },
    { key: 'checked_out', label: 'Checked Out', count: dashboard?.checked_out },
    { key: 'replacement', label: 'Replacement Due', count: dashboard?.replacement_due },
    { key: 'inspection', label: 'Inspection Due', count: dashboard?.inspection_due },
  ];

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Tool & Fixture Management</h1>
        <button onClick={() => setShowCreateModal(true)} className="inline-flex items-center px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700">
          <PlusIcon className="w-5 h-5 mr-2" />New Tool
        </button>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-blue-500">
          <div className="text-sm text-slate-400">Total Tools</div>
          <div className="text-2xl font-bold text-white">{dashboard?.total_tools || 0}</div>
        </div>
        <div className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-indigo-500">
          <div className="text-sm text-slate-400">Checked Out</div>
          <div className="text-2xl font-bold text-indigo-600">{dashboard?.checked_out || 0}</div>
        </div>
        <div className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-orange-500">
          <div className="text-sm text-slate-400">Replacement Due</div>
          <div className="text-2xl font-bold text-orange-600">{dashboard?.replacement_due || 0}</div>
        </div>
        <div className="bg-[#151b28] rounded-lg shadow p-4 border-l-4 border-red-500">
          <div className="text-sm text-slate-400">Inspection Due</div>
          <div className="text-2xl font-bold text-red-600">{dashboard?.inspection_due || 0}</div>
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-slate-700">
        <nav className="flex -mb-px space-x-6">
          {tabs.map(tab => (
            <button key={tab.key} onClick={() => setActiveTab(tab.key)}
              className={`py-3 px-1 border-b-2 text-sm font-medium ${activeTab === tab.key ? 'border-blue-500 text-blue-600' : 'border-transparent text-slate-400 hover:text-slate-300'}`}>
              {tab.label}{tab.count !== undefined && <span className="ml-1 text-xs bg-slate-800/50 text-slate-400 rounded-full px-2 py-0.5">{tab.count}</span>}
            </button>
          ))}
        </nav>
      </div>

      {/* Filters */}
      <div className="flex gap-3 flex-wrap">
        <div className="relative flex-1 min-w-[200px]">
          <MagnifyingGlassIcon className="absolute left-3 top-2.5 w-5 h-5 text-slate-400" />
          <input type="text" placeholder="Search tools..." value={search} onChange={e => setSearch(e.target.value)}
            className="w-full pl-10 pr-4 py-2 border border-slate-600 rounded-lg focus:ring-2 focus:ring-blue-500" />
        </div>
        <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} className="px-3 py-2 border border-slate-600 rounded-lg">
          <option value="">All Statuses</option>
          <option value="available">Available</option>
          <option value="checked_out">Checked Out</option>
          <option value="maintenance">Maintenance</option>
          <option value="needs_repair">Needs Repair</option>
          <option value="retired">Retired</option>
        </select>
        <select value={typeFilter} onChange={e => setTypeFilter(e.target.value)} className="px-3 py-2 border border-slate-600 rounded-lg">
          <option value="">All Types</option>
          <option value="cutting_tool">Cutting Tool</option>
          <option value="fixture">Fixture</option>
          <option value="jig">Jig</option>
          <option value="gauge">Gauge</option>
          <option value="die">Die</option>
          <option value="mold">Mold</option>
          <option value="other">Other</option>
        </select>
        <button onClick={loadData} className="p-2 text-slate-400 hover:text-slate-300"><ArrowPathIcon className="w-5 h-5" /></button>
      </div>

      {/* Table */}
      <div className="bg-[#151b28] rounded-lg shadow overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-800 text-left text-xs font-medium text-slate-400 uppercase">
            <tr>
              <th className="px-4 py-3">Tool #</th>
              <th className="px-4 py-3">Name</th>
              <th className="px-4 py-3">Type</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Location</th>
              <th className="px-4 py-3">Uses</th>
              <th className="px-4 py-3">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-700">
            {filteredTools.length === 0 ? (
              <tr><td colSpan={7} className="px-4 py-12 text-center text-slate-400">No tools found</td></tr>
            ) : filteredTools.map(tool => (
              <React.Fragment key={tool.id}>
                <tr className="hover:bg-slate-800 cursor-pointer" onClick={() => toggleExpand(tool.id)}>
                  <td className="px-4 py-3 font-medium text-blue-600">{tool.tool_number}</td>
                  <td className="px-4 py-3">{tool.name}</td>
                  <td className="px-4 py-3 capitalize">{tool.tool_type?.replace(/_/g, ' ')}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${statusColors[tool.status]?.bg || 'bg-slate-800/50'} ${statusColors[tool.status]?.text || 'text-slate-100'}`}>
                      {tool.status?.replace(/_/g, ' ')}
                    </span>
                  </td>
                  <td className="px-4 py-3">{tool.location || '-'}</td>
                  <td className="px-4 py-3">{tool.current_uses}{tool.max_uses ? ` / ${tool.max_uses}` : ''}</td>
                  <td className="px-4 py-3" onClick={e => e.stopPropagation()}>
                    <div className="flex gap-1">
                      {tool.status === 'available' && (
                        <button onClick={() => { setSelectedTool(tool); setShowCheckoutModal(true); }}
                          className="text-xs px-2 py-1 bg-blue-500/100 text-white rounded hover:bg-blue-600" title="Checkout">
                          <ArrowRightOnRectangleIcon className="w-4 h-4" />
                        </button>
                      )}
                      {(tool.status === 'checked_out' || tool.status === 'in_use') && (
                        <button onClick={() => { setSelectedTool(tool); setShowCheckinModal(true); }}
                          className="text-xs px-2 py-1 bg-green-500/100 text-white rounded hover:bg-green-600" title="Check In">
                          <ArrowLeftOnRectangleIcon className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
                {expandedId === tool.id && (
                  <tr>
                    <td colSpan={7} className="px-4 py-4 bg-slate-800">
                      <div className="grid grid-cols-3 gap-4">
                        <div>
                          <h4 className="font-medium text-slate-300 mb-2">Details</h4>
                          <div className="space-y-1 text-sm">
                            <div><span className="text-slate-400">Manufacturer:</span> {tool.manufacturer || '-'}</div>
                            <div><span className="text-slate-400">Model:</span> {tool.model_number || '-'}</div>
                            <div><span className="text-slate-400">Serial:</span> {tool.serial_number || '-'}</div>
                            <div><span className="text-slate-400">Purchase Cost:</span> {tool.purchase_cost ? `$${tool.purchase_cost.toFixed(2)}` : '-'}</div>
                            <div><span className="text-slate-400">Purchase Date:</span> {tool.purchase_date ? new Date(tool.purchase_date).toLocaleDateString() : '-'}</div>
                          </div>
                        </div>
                        <div>
                          <h4 className="font-medium text-slate-300 mb-2">Usage & Life</h4>
                          <div className="space-y-1 text-sm">
                            <div><span className="text-slate-400">Uses:</span> {tool.current_uses}{tool.max_uses ? ` / ${tool.max_uses}` : ''}</div>
                            <div><span className="text-slate-400">Life Hours:</span> {tool.current_life_hours.toFixed(1)}{tool.max_life_hours ? ` / ${tool.max_life_hours}` : ''} hrs</div>
                            <div><span className="text-slate-400">Last Inspection:</span> {tool.last_inspection_date ? new Date(tool.last_inspection_date).toLocaleDateString() : '-'}</div>
                            <div><span className="text-slate-400">Next Inspection:</span> {tool.next_inspection_date ? new Date(tool.next_inspection_date).toLocaleDateString() : '-'}</div>
                            {tool.notes && <div><span className="text-slate-400">Notes:</span> {tool.notes}</div>}
                          </div>
                        </div>
                        <div>
                          <h4 className="font-medium text-slate-300 mb-2">Recent History</h4>
                          {toolHistory.length === 0 ? (
                            <p className="text-sm text-slate-400">No history</p>
                          ) : (
                            <div className="space-y-2 max-h-40 overflow-y-auto">
                              {toolHistory.slice(0, 10).map((h: any, i: number) => (
                                <div key={i} className="text-xs border-l-2 border-slate-600 pl-2">
                                  <div className="font-medium">{h.action || h.event_type}</div>
                                  <div className="text-slate-400">{h.created_at ? new Date(h.created_at).toLocaleString() : ''}</div>
                                  {h.notes && <div className="text-slate-400">{h.notes}</div>}
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </div>

      {/* Create Tool Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-[#151b28] rounded-lg shadow-xl w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center p-4 border-b">
              <h3 className="text-lg font-semibold">New Tool</h3>
              <button onClick={() => setShowCreateModal(false)}><XMarkIcon className="w-5 h-5" /></button>
            </div>
            <div className="p-4 space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Tool Number *</label>
                  <input type="text" value={createForm.tool_number} onChange={e => setCreateForm(f => ({ ...f, tool_number: e.target.value }))}
                    className="w-full px-3 py-2 border rounded-lg" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Name *</label>
                  <input type="text" value={createForm.name} onChange={e => setCreateForm(f => ({ ...f, name: e.target.value }))}
                    className="w-full px-3 py-2 border rounded-lg" />
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Type</label>
                <select value={createForm.tool_type} onChange={e => setCreateForm(f => ({ ...f, tool_type: e.target.value }))} className="w-full px-3 py-2 border rounded-lg">
                  <option value="cutting_tool">Cutting Tool</option>
                  <option value="fixture">Fixture</option>
                  <option value="jig">Jig</option>
                  <option value="gauge">Gauge</option>
                  <option value="die">Die</option>
                  <option value="mold">Mold</option>
                  <option value="other">Other</option>
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Description</label>
                <textarea value={createForm.description} onChange={e => setCreateForm(f => ({ ...f, description: e.target.value }))} rows={2} className="w-full px-3 py-2 border rounded-lg" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Location</label>
                  <input type="text" value={createForm.location} onChange={e => setCreateForm(f => ({ ...f, location: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Manufacturer</label>
                  <input type="text" value={createForm.manufacturer} onChange={e => setCreateForm(f => ({ ...f, manufacturer: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                </div>
              </div>
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Purchase Cost</label>
                  <input type="number" value={createForm.purchase_cost} onChange={e => setCreateForm(f => ({ ...f, purchase_cost: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Max Uses</label>
                  <input type="number" value={createForm.max_uses} onChange={e => setCreateForm(f => ({ ...f, max_uses: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1">Max Life (hrs)</label>
                  <input type="number" value={createForm.max_life_hours} onChange={e => setCreateForm(f => ({ ...f, max_life_hours: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                </div>
              </div>
            </div>
            <div className="flex justify-end gap-2 p-4 border-t">
              <button onClick={() => setShowCreateModal(false)} className="px-4 py-2 border rounded-lg hover:bg-slate-800">Cancel</button>
              <button onClick={handleCreate} disabled={!createForm.tool_number || !createForm.name}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50">Create</button>
            </div>
          </div>
        </div>
      )}

      {/* Checkout Modal */}
      {showCheckoutModal && selectedTool && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-[#151b28] rounded-lg shadow-xl w-full max-w-md mx-4">
            <div className="flex justify-between items-center p-4 border-b">
              <h3 className="text-lg font-semibold">Checkout: {selectedTool.tool_number}</h3>
              <button onClick={() => setShowCheckoutModal(false)}><XMarkIcon className="w-5 h-5" /></button>
            </div>
            <div className="p-4 space-y-3">
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Checked Out To *</label>
                <input type="text" value={checkoutForm.checked_out_to} onChange={e => setCheckoutForm(f => ({ ...f, checked_out_to: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" placeholder="Operator name or ID" />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Work Order ID</label>
                <input type="number" value={checkoutForm.work_order_id} onChange={e => setCheckoutForm(f => ({ ...f, work_order_id: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Notes</label>
                <textarea value={checkoutForm.notes} onChange={e => setCheckoutForm(f => ({ ...f, notes: e.target.value }))} rows={2} className="w-full px-3 py-2 border rounded-lg" />
              </div>
            </div>
            <div className="flex justify-end gap-2 p-4 border-t">
              <button onClick={() => setShowCheckoutModal(false)} className="px-4 py-2 border rounded-lg">Cancel</button>
              <button onClick={handleCheckout} disabled={!checkoutForm.checked_out_to} className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50">Checkout</button>
            </div>
          </div>
        </div>
      )}

      {/* Checkin Modal */}
      {showCheckinModal && selectedTool && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-[#151b28] rounded-lg shadow-xl w-full max-w-md mx-4">
            <div className="flex justify-between items-center p-4 border-b">
              <h3 className="text-lg font-semibold">Check In: {selectedTool.tool_number}</h3>
              <button onClick={() => setShowCheckinModal(false)}><XMarkIcon className="w-5 h-5" /></button>
            </div>
            <div className="p-4 space-y-3">
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Condition</label>
                <select value={checkinForm.condition} onChange={e => setCheckinForm(f => ({ ...f, condition: e.target.value }))} className="w-full px-3 py-2 border rounded-lg">
                  <option value="good">Good</option>
                  <option value="worn">Worn</option>
                  <option value="damaged">Damaged</option>
                  <option value="needs_repair">Needs Repair</option>
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Uses This Session</label>
                <input type="number" value={checkinForm.uses_this_session} onChange={e => setCheckinForm(f => ({ ...f, uses_this_session: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-1">Notes</label>
                <textarea value={checkinForm.notes} onChange={e => setCheckinForm(f => ({ ...f, notes: e.target.value }))} rows={2} className="w-full px-3 py-2 border rounded-lg" />
              </div>
            </div>
            <div className="flex justify-end gap-2 p-4 border-t">
              <button onClick={() => setShowCheckinModal(false)} className="px-4 py-2 border rounded-lg">Cancel</button>
              <button onClick={handleCheckin} className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700">Check In</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
