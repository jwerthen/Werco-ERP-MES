import React, { useState, useEffect, useMemo, useCallback } from 'react';
import api from '../services/api';
import {
  PlusIcon,
  MagnifyingGlassIcon,
  ArrowPathIcon,
  XMarkIcon,
  ArrowRightOnRectangleIcon,
  ArrowLeftOnRectangleIcon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline';
import { Modal } from '../components/ui/Modal';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { formatCentralDate, formatCentralDateTime } from '../utils/centralTime';
import {
  ErrorState,
  useToast,
  StatusBadge,
  DataTable,
  DataTableColumn,
  MobileDataCard,
  Button,
  FormField,
} from '../components/ui';

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

export default function ToolManagement() {
  const { showToast } = useToast();
  const [tools, setTools] = useState<Tool[]>([]);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [tabError, setTabError] = useState(false);
  const [search, setSearch] = useState('');
  const debouncedSearch = useDebouncedValue(search, 250);
  const [statusFilter, setStatusFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [activeTab, setActiveTab] = useState<Tab>('all');
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showCheckoutModal, setShowCheckoutModal] = useState(false);
  const [showCheckinModal, setShowCheckinModal] = useState(false);
  const [selectedTool, setSelectedTool] = useState<Tool | null>(null);
  const [detailTool, setDetailTool] = useState<Tool | null>(null);
  const [showDetailModal, setShowDetailModal] = useState(false);
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
      setTabError(false);
      let data: Tool[] = [];
      if (tab === 'checked_out') data = await api.getToolsCheckedOut();
      else if (tab === 'replacement') data = await api.getToolsReplacementDue();
      else if (tab === 'inspection') data = await api.getToolsInspectionDue();
      else data = await api.getTools({ include_inactive: false });
      setTools(data || []);
    } catch (err) {
      console.error('Failed to load tab data:', err);
      setTabError(true);
    }
  }, []);

  useEffect(() => { loadTabData(activeTab); }, [activeTab, loadTabData]);

  const filteredTools = useMemo(() => {
    return tools.filter(t => {
      if (statusFilter && t.status !== statusFilter) return false;
      if (typeFilter && t.tool_type !== typeFilter) return false;
      if (debouncedSearch) {
        const s = debouncedSearch.toLowerCase();
        return (
          t.tool_number?.toLowerCase().includes(s) ||
          t.name?.toLowerCase().includes(s) ||
          t.serial_number?.toLowerCase().includes(s) ||
          t.location?.toLowerCase().includes(s)
        );
      }
      return true;
    });
  }, [tools, statusFilter, typeFilter, debouncedSearch]);

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
      showToast('error', err.response?.data?.detail || 'Failed to create tool');
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
      showToast('error', err.response?.data?.detail || 'Failed to checkout tool');
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
      showToast('error', err.response?.data?.detail || 'Failed to checkin tool');
    }
  };

  const openDetail = async (tool: Tool) => {
    setDetailTool(tool);
    setShowDetailModal(true);
    setToolHistory([]);
    try {
      const history = await api.getToolHistory(tool.id);
      setToolHistory(history || []);
    } catch { setToolHistory([]); }
  };

  const renderRowActions = (tool: Tool) => (
    <div className="flex gap-1">
      {tool.status === 'available' && (
        <button
          onClick={(e) => { e.stopPropagation(); setSelectedTool(tool); setShowCheckoutModal(true); }}
          className="text-xs px-2 py-1 bg-blue-500/100 text-white rounded hover:bg-blue-600"
          title="Checkout"
          aria-label="Checkout tool"
        >
          <ArrowRightOnRectangleIcon className="w-4 h-4" aria-hidden="true" />
        </button>
      )}
      {(tool.status === 'checked_out' || tool.status === 'in_use') && (
        <button
          onClick={(e) => { e.stopPropagation(); setSelectedTool(tool); setShowCheckinModal(true); }}
          className="text-xs px-2 py-1 bg-green-500/100 text-white rounded hover:bg-green-600"
          title="Check In"
          aria-label="Check in tool"
        >
          <ArrowLeftOnRectangleIcon className="w-4 h-4" aria-hidden="true" />
        </button>
      )}
    </div>
  );

  const columns: Array<DataTableColumn<Tool>> = [
    {
      key: 'tool_number',
      header: 'Tool #',
      sortable: true,
      accessor: (t) => t.tool_number,
      className: 'font-medium text-blue-400',
    },
    {
      key: 'name',
      header: 'Name',
      sortable: true,
      accessor: (t) => t.name,
    },
    {
      key: 'tool_type',
      header: 'Type',
      sortable: true,
      accessor: (t) => t.tool_type,
      className: 'capitalize',
      render: (t) => t.tool_type?.replace(/_/g, ' '),
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (t) => t.status,
      render: (t) => <StatusBadge status={t.status} />,
    },
    {
      key: 'location',
      header: 'Location',
      sortable: true,
      accessor: (t) => t.location ?? '',
      render: (t) => t.location || '-',
    },
    {
      key: 'uses',
      header: 'Uses',
      sortable: true,
      align: 'right',
      accessor: (t) => t.current_uses,
      csv: (t) => `${t.current_uses}${t.max_uses ? ` / ${t.max_uses}` : ''}`,
      render: (t) => `${t.current_uses}${t.max_uses ? ` / ${t.max_uses}` : ''}`,
    },
    {
      key: 'actions',
      header: 'Actions',
      render: (t) => renderRowActions(t),
    },
  ];

  const renderToolCard = (tool: Tool) => (
    <MobileDataCard
      title={tool.tool_number}
      subtitle={tool.name}
      badge={<StatusBadge status={tool.status} />}
      onClick={() => openDetail(tool)}
      fields={[
        { label: 'Type', value: <span className="capitalize">{tool.tool_type?.replace(/_/g, ' ')}</span> },
        { label: 'Location', value: tool.location || '-' },
        { label: 'Uses', value: `${tool.current_uses}${tool.max_uses ? ` / ${tool.max_uses}` : ''}` },
      ]}
      actions={renderRowActions(tool)}
    />
  );

  if (loading) {
    return <div className="p-6"><div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/4" /><div className="grid grid-cols-4 gap-4">{[...Array(4)].map((_, i) => <div key={i} className="h-24 bg-gray-200 rounded" />)}</div><div className="h-64 bg-gray-200 rounded" /></div></div>;
  }

  if (error) {
    return (
      <div className="p-6">
        <ErrorState message={error} onRetry={loadData} />
      </div>
    );
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
        <Button onClick={() => setShowCreateModal(true)} className="inline-flex items-center">
          <PlusIcon className="w-5 h-5 mr-2" />New Tool
        </Button>
      </div>

      {/* Tabs (counts carry the KPI summary — see badges) */}
      <div className="border-b border-fd-line">
        <nav className="flex -mb-px space-x-6">
          {tabs.map(tab => (
            <button key={tab.key} onClick={() => setActiveTab(tab.key)}
              className={`py-3 px-1 border-b-2 text-sm font-medium ${activeTab === tab.key ? 'border-fd-blue text-fd-blue' : 'border-transparent text-slate-400 hover:text-slate-300'}`}>
              {tab.label}{tab.count !== undefined && <span className="ml-1.5 text-xs tabular-nums bg-fd-panel border border-fd-line text-slate-300 rounded-sm px-1.5 py-0.5">{tab.count}</span>}
            </button>
          ))}
        </nav>
      </div>

      {/* Filters */}
      <div className="flex gap-3 flex-wrap">
        <div className="relative flex-1 min-w-[200px]">
          <MagnifyingGlassIcon className="absolute left-3 top-2.5 w-5 h-5 text-slate-400" />
          <input type="text" placeholder="Search tools..." aria-label="Search tools" value={search} onChange={e => setSearch(e.target.value)}
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
        <button onClick={loadData} className="p-2 text-slate-400 hover:text-slate-300" title="Refresh" aria-label="Refresh tools"><ArrowPathIcon className="w-5 h-5" aria-hidden="true" /></button>
      </div>

      {/* Table */}
      <DataTable
        columns={columns}
        data={filteredTools}
        rowKey={(t) => t.id}
        onRowClick={openDetail}
        defaultSort={{ key: 'tool_number', dir: 'asc' }}
        pageSize={25}
        csvExport={{ filename: 'tools' }}
        error={tabError}
        onRetry={() => loadTabData(activeTab)}
        empty={{
          icon: WrenchScrewdriverIcon,
          title: 'No tools found',
          description:
            search || statusFilter || typeFilter
              ? 'No tools match the current filters.'
              : 'Tools and fixtures you add will appear here.',
          action: { label: 'New Tool', onClick: () => setShowCreateModal(true) },
        }}
        mobileCards={renderToolCard}
      />

      {/* Tool Detail Modal */}
      <Modal
        open={showDetailModal && !!detailTool}
        onClose={() => setShowDetailModal(false)}
        size="lg"
        padded={false}
      >
        {detailTool && (
          <>
            <div className="flex justify-between items-center p-4 border-b">
              <div className="flex items-center gap-3">
                <h3 className="text-lg font-semibold">{detailTool.tool_number} — {detailTool.name}</h3>
                <StatusBadge status={detailTool.status} />
              </div>
              <button onClick={() => setShowDetailModal(false)} aria-label="Close"><XMarkIcon className="w-5 h-5" aria-hidden="true" /></button>
            </div>
            <div className="p-4 grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <h4 className="font-medium text-slate-300 mb-2">Details</h4>
                <div className="space-y-1 text-sm">
                  <div><span className="text-slate-400">Manufacturer:</span> {detailTool.manufacturer || '-'}</div>
                  <div><span className="text-slate-400">Model:</span> {detailTool.model_number || '-'}</div>
                  <div><span className="text-slate-400">Serial:</span> {detailTool.serial_number || '-'}</div>
                  <div><span className="text-slate-400">Purchase Cost:</span> {detailTool.purchase_cost ? `$${detailTool.purchase_cost.toFixed(2)}` : '-'}</div>
                  <div><span className="text-slate-400">Purchase Date:</span> {detailTool.purchase_date ? formatCentralDate(detailTool.purchase_date) : '-'}</div>
                </div>
              </div>
              <div>
                <h4 className="font-medium text-slate-300 mb-2">Usage &amp; Life</h4>
                <div className="space-y-1 text-sm">
                  <div><span className="text-slate-400">Uses:</span> {detailTool.current_uses}{detailTool.max_uses ? ` / ${detailTool.max_uses}` : ''}</div>
                  <div><span className="text-slate-400">Life Hours:</span> {detailTool.current_life_hours.toFixed(1)}{detailTool.max_life_hours ? ` / ${detailTool.max_life_hours}` : ''} hrs</div>
                  <div><span className="text-slate-400">Last Inspection:</span> {detailTool.last_inspection_date ? formatCentralDate(detailTool.last_inspection_date) : '-'}</div>
                  <div><span className="text-slate-400">Next Inspection:</span> {detailTool.next_inspection_date ? formatCentralDate(detailTool.next_inspection_date) : '-'}</div>
                  {detailTool.notes && <div><span className="text-slate-400">Notes:</span> {detailTool.notes}</div>}
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
                        <div className="text-slate-400">{h.created_at ? formatCentralDateTime(h.created_at) : ''}</div>
                        {h.notes && <div className="text-slate-400">{h.notes}</div>}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
            <div className="flex justify-end gap-2 p-4 border-t">
              {detailTool.status === 'available' && (
                <Button
                  onClick={() => { setSelectedTool(detailTool); setShowDetailModal(false); setShowCheckoutModal(true); }}
                >Checkout</Button>
              )}
              {(detailTool.status === 'checked_out' || detailTool.status === 'in_use') && (
                <button
                  onClick={() => { setSelectedTool(detailTool); setShowDetailModal(false); setShowCheckinModal(true); }}
                  className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700"
                >Check In</button>
              )}
              <Button variant="secondary" onClick={() => setShowDetailModal(false)}>Close</Button>
            </div>
          </>
        )}
      </Modal>

      {/* Create Tool Modal */}
      <Modal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        size="lg"
        closeOnBackdrop={false}
        padded={false}
      >
            <div className="flex justify-between items-center p-4 border-b">
              <h3 className="text-lg font-semibold">New Tool</h3>
              <button onClick={() => setShowCreateModal(false)} aria-label="Close"><XMarkIcon className="w-5 h-5" aria-hidden="true" /></button>
            </div>
            <div className="p-4 space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <FormField label="Tool Number" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input {...field} type="text" value={createForm.tool_number} onChange={e => setCreateForm(f => ({ ...f, tool_number: e.target.value }))}
                      className="w-full px-3 py-2 border rounded-lg" />
                  )}
                </FormField>
                <FormField label="Name" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input {...field} type="text" value={createForm.name} onChange={e => setCreateForm(f => ({ ...f, name: e.target.value }))}
                      className="w-full px-3 py-2 border rounded-lg" />
                  )}
                </FormField>
              </div>
              <FormField label="Type" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <select {...field} value={createForm.tool_type} onChange={e => setCreateForm(f => ({ ...f, tool_type: e.target.value }))} className="w-full px-3 py-2 border rounded-lg">
                    <option value="cutting_tool">Cutting Tool</option>
                    <option value="fixture">Fixture</option>
                    <option value="jig">Jig</option>
                    <option value="gauge">Gauge</option>
                    <option value="die">Die</option>
                    <option value="mold">Mold</option>
                    <option value="other">Other</option>
                  </select>
                )}
              </FormField>
              <FormField label="Description" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <textarea {...field} value={createForm.description} onChange={e => setCreateForm(f => ({ ...f, description: e.target.value }))} rows={2} className="w-full px-3 py-2 border rounded-lg" />
                )}
              </FormField>
              <div className="grid grid-cols-2 gap-3">
                <FormField label="Location" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input {...field} type="text" value={createForm.location} onChange={e => setCreateForm(f => ({ ...f, location: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                  )}
                </FormField>
                <FormField label="Manufacturer" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input {...field} type="text" value={createForm.manufacturer} onChange={e => setCreateForm(f => ({ ...f, manufacturer: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                  )}
                </FormField>
              </div>
              <div className="grid grid-cols-3 gap-3">
                <FormField label="Purchase Cost" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input {...field} type="number" value={createForm.purchase_cost} onChange={e => setCreateForm(f => ({ ...f, purchase_cost: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                  )}
                </FormField>
                <FormField label="Max Uses" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input {...field} type="number" value={createForm.max_uses} onChange={e => setCreateForm(f => ({ ...f, max_uses: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                  )}
                </FormField>
                <FormField label="Max Life (hrs)" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input {...field} type="number" value={createForm.max_life_hours} onChange={e => setCreateForm(f => ({ ...f, max_life_hours: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                  )}
                </FormField>
              </div>
            </div>
            <div className="flex justify-end gap-2 p-4 border-t">
              <Button variant="secondary" onClick={() => setShowCreateModal(false)}>Cancel</Button>
              <Button onClick={handleCreate} disabled={!createForm.tool_number || !createForm.name}>Create</Button>
            </div>
      </Modal>

      {/* Checkout Modal */}
      <Modal
        open={showCheckoutModal && !!selectedTool}
        onClose={() => setShowCheckoutModal(false)}
        size="md"
        closeOnBackdrop={false}
        padded={false}
      >
        {selectedTool && (
          <>
            <div className="flex justify-between items-center p-4 border-b">
              <h3 className="text-lg font-semibold">Checkout: {selectedTool.tool_number}</h3>
              <button onClick={() => setShowCheckoutModal(false)} aria-label="Close"><XMarkIcon className="w-5 h-5" aria-hidden="true" /></button>
            </div>
            <div className="p-4 space-y-3">
              <FormField label="Checked Out To" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <input {...field} type="text" value={checkoutForm.checked_out_to} onChange={e => setCheckoutForm(f => ({ ...f, checked_out_to: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" placeholder="Operator name or ID" />
                )}
              </FormField>
              <FormField label="Work Order ID" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <input {...field} type="number" value={checkoutForm.work_order_id} onChange={e => setCheckoutForm(f => ({ ...f, work_order_id: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                )}
              </FormField>
              <FormField label="Notes" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <textarea {...field} value={checkoutForm.notes} onChange={e => setCheckoutForm(f => ({ ...f, notes: e.target.value }))} rows={2} className="w-full px-3 py-2 border rounded-lg" />
                )}
              </FormField>
            </div>
            <div className="flex justify-end gap-2 p-4 border-t">
              <Button variant="secondary" onClick={() => setShowCheckoutModal(false)}>Cancel</Button>
              <Button onClick={handleCheckout} disabled={!checkoutForm.checked_out_to}>Checkout</Button>
            </div>
          </>
        )}
      </Modal>

      {/* Checkin Modal */}
      <Modal
        open={showCheckinModal && !!selectedTool}
        onClose={() => setShowCheckinModal(false)}
        size="md"
        closeOnBackdrop={false}
        padded={false}
      >
        {selectedTool && (
          <>
            <div className="flex justify-between items-center p-4 border-b">
              <h3 className="text-lg font-semibold">Check In: {selectedTool.tool_number}</h3>
              <button onClick={() => setShowCheckinModal(false)} aria-label="Close"><XMarkIcon className="w-5 h-5" aria-hidden="true" /></button>
            </div>
            <div className="p-4 space-y-3">
              <FormField label="Condition" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <select {...field} value={checkinForm.condition} onChange={e => setCheckinForm(f => ({ ...f, condition: e.target.value }))} className="w-full px-3 py-2 border rounded-lg">
                    <option value="good">Good</option>
                    <option value="worn">Worn</option>
                    <option value="damaged">Damaged</option>
                    <option value="needs_repair">Needs Repair</option>
                  </select>
                )}
              </FormField>
              <FormField label="Uses This Session" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <input {...field} type="number" value={checkinForm.uses_this_session} onChange={e => setCheckinForm(f => ({ ...f, uses_this_session: e.target.value }))} className="w-full px-3 py-2 border rounded-lg" />
                )}
              </FormField>
              <FormField label="Notes" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <textarea {...field} value={checkinForm.notes} onChange={e => setCheckinForm(f => ({ ...f, notes: e.target.value }))} rows={2} className="w-full px-3 py-2 border rounded-lg" />
                )}
              </FormField>
            </div>
            <div className="flex justify-end gap-2 p-4 border-t">
              <Button variant="secondary" onClick={() => setShowCheckinModal(false)}>Cancel</Button>
              <button onClick={handleCheckin} className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700">Check In</button>
            </div>
          </>
        )}
      </Modal>
    </div>
  );
}
