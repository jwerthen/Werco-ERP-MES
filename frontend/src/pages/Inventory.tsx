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
import { Modal } from '../components/ui/Modal';
import {
  ErrorState,
  useToast,
  DataTable,
  DataTableColumn,
  MobileDataCard,
} from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { useDebouncedValue } from '../hooks/useDebouncedValue';

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

export default function InventoryPage({ embedded }: { embedded?: boolean }) {
  const { showToast } = useToast();
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
  const [loadError, setLoadError] = useState(false);
  const [parts, setParts] = useState<any[]>([]);
  const [locations, setLocations] = useState<any[]>([]);
  const [showLowStockOnly, setShowLowStockOnly] = useState(() => searchParams.get('filter') === 'low_stock');
  const [filterText, setFilterText] = useState('');
  const debouncedFilterText = useDebouncedValue(filterText, 250);
  
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
    if (!debouncedFilterText) return grouped;
    const term = debouncedFilterText.toLowerCase();
    return grouped.filter((item) => (
      item.part_number?.toLowerCase().includes(term) ||
      item.part_name?.toLowerCase().includes(term)
    ));
  }, [debouncedFilterText, filterByGroup, getPartType, lowStockPartIds, showLowStockOnly, summary]);
  const groupSummary = useMemo(
    () => summary.filter((item) => filterByGroup(getPartType(item.part_id))),
    [filterByGroup, getPartType, summary]
  );
  const filteredInventory = useMemo(() => {
    const grouped = inventory.filter((item) => filterByGroup(item.part?.part_type));
    if (!debouncedFilterText) return grouped;
    const term = debouncedFilterText.toLowerCase();
    return grouped.filter((item) => (
      item.part?.part_number?.toLowerCase().includes(term) ||
      item.part?.name?.toLowerCase().includes(term)
    ));
  }, [filterByGroup, debouncedFilterText, inventory]);
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
    setLoadError(false);
    try {
      const [invRes, summaryRes, partsRes, locsRes, lowStockRes] = await Promise.all([
        api.getInventory(),
        api.getInventorySummary(),
        api.getParts({ active_only: true, item_group: 'all' }),
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
      setLoadError(true);
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
      showToast('error', err.response?.data?.detail || 'Failed to receive inventory');
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
      showToast('error', err.response?.data?.detail || 'Failed to transfer inventory');
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

  const renderTypeBadge = (partType?: string) => (
    <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium ${
      PART_TYPES.has(partType || '')
        ? 'bg-blue-500/20 text-werco-navy-700'
        : 'bg-amber-500/20 text-amber-400'
    }`}>
      {getPartTypeIcon(partType)}
      {getPartTypeLabel(partType)}
    </span>
  );

  // ---- Summary tab (by part) columns ----
  const summaryColumns = useMemo<Array<DataTableColumn<InventorySummary>>>(() => [
    {
      key: 'part',
      header: 'Part',
      sortable: true,
      accessor: (item) => item.part_number,
      csv: (item) => `${item.part_number} ${item.part_name}`.trim(),
      render: (item) => {
        const isLowStock = lowStockPartIds.has(item.part_id);
        return (
          <div>
            <div className="font-medium">{item.part_number}</div>
            <div className="text-sm text-slate-400">{item.part_name}</div>
            {isLowStock && <span className="text-xs text-red-600 font-medium">LOW STOCK</span>}
          </div>
        );
      },
    },
    {
      key: 'type',
      header: 'Type',
      sortable: true,
      accessor: (item) => getPartTypeLabel(getPartType(item.part_id)),
      render: (item) => renderTypeBadge(getPartType(item.part_id)),
    },
    {
      key: 'on_hand',
      header: 'On Hand',
      sortable: true,
      align: 'right',
      className: 'font-medium',
      accessor: (item) => item.total_on_hand,
    },
    {
      key: 'allocated',
      header: 'Allocated',
      sortable: true,
      align: 'right',
      accessor: (item) => item.total_allocated,
    },
    {
      key: 'available',
      header: 'Available',
      sortable: true,
      align: 'right',
      className: 'text-green-600 font-medium',
      accessor: (item) => item.available,
    },
    {
      key: 'locations',
      header: 'Locations',
      csv: (item) =>
        item.locations
          .map((loc) => `${loc.location} (${loc.quantity})${loc.lot_number ? ` Lot:${loc.lot_number}` : ''}`)
          .join('; '),
      render: (item) => (
        <>
          {item.locations.map((loc, idx) => (
            <div key={idx} className="text-sm">
              <span className="font-mono bg-slate-800/50 px-1 rounded">{loc.location}</span>
              <span className="text-slate-400 ml-2">({loc.quantity})</span>
              {loc.lot_number && <span className="text-slate-400 ml-1">Lot: {loc.lot_number}</span>}
            </div>
          ))}
        </>
      ),
    },
  ], [lowStockPartIds, getPartType]);

  // ---- Detail tab (by location) columns ----
  const detailColumns = useMemo<Array<DataTableColumn<InventoryItem>>>(() => [
    {
      key: 'part',
      header: 'Part',
      sortable: true,
      accessor: (item) => item.part?.part_number ?? '',
      csv: (item) => `${item.part?.part_number ?? ''} ${item.part?.name ?? ''}`.trim(),
      render: (item) => (
        <div>
          <div className="font-medium">{item.part?.part_number}</div>
          <div className="text-xs text-slate-400">{item.part?.name}</div>
        </div>
      ),
    },
    {
      key: 'type',
      header: 'Type',
      sortable: true,
      accessor: (item) => getPartTypeLabel(item.part?.part_type),
      render: (item) => renderTypeBadge(item.part?.part_type),
    },
    {
      key: 'location',
      header: 'Location',
      sortable: true,
      className: 'font-mono text-sm',
      accessor: (item) => item.location,
    },
    {
      key: 'lot',
      header: 'Lot #',
      sortable: true,
      className: 'text-sm',
      accessor: (item) => item.lot_number ?? '',
      render: (item) => item.lot_number || '-',
    },
    {
      key: 'qty',
      header: 'Qty',
      sortable: true,
      align: 'right',
      className: 'font-medium',
      accessor: (item) => item.quantity_on_hand,
    },
    {
      key: 'available',
      header: 'Available',
      sortable: true,
      align: 'right',
      className: 'text-green-600',
      accessor: (item) => item.quantity_available,
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (item) => item.status,
      render: (item) => (
        <span className={`px-2 py-1 rounded text-xs ${
          item.status === 'available' ? 'bg-green-500/20 text-emerald-300' :
          item.status === 'quarantine' ? 'bg-yellow-500/20 text-yellow-300' :
          'bg-slate-800/50 text-slate-100'
        }`}>{item.status}</span>
      ),
    },
    {
      key: 'actions',
      header: 'Actions',
      align: 'center',
      render: (item) => (
        <button
          onClick={(e) => { e.stopPropagation(); openTransfer(item); }}
          className="text-werco-primary hover:text-blue-400"
          aria-label="Transfer inventory"
        >
          <ArrowsRightLeftIcon className="h-5 w-5" aria-hidden="true" />
        </button>
      ),
    },
  ], []);

  // ---- Mobile cards (below md) ----
  const renderSummaryCard = (item: InventorySummary) => {
    const isLowStock = lowStockPartIds.has(item.part_id);
    return (
      <MobileDataCard
        key={item.part_id}
        title={item.part_number}
        subtitle={item.part_name}
        badge={renderTypeBadge(getPartType(item.part_id))}
        highlight={isLowStock}
        fields={[
          {
            label: 'On Hand',
            value: item.total_on_hand,
            className: 'font-medium',
          },
          { label: 'Allocated', value: item.total_allocated },
          {
            label: 'Available',
            value: <span className="text-green-600 font-medium">{item.available}</span>,
          },
          {
            label: 'Status',
            value: isLowStock ? (
              <span className="text-xs text-red-600 font-medium">LOW STOCK</span>
            ) : (
              <span className="text-slate-400">—</span>
            ),
          },
          {
            label: 'Locations',
            fullWidth: true,
            value: item.locations.length ? (
              <div className="space-y-0.5">
                {item.locations.map((loc, idx) => (
                  <div key={idx} className="text-sm">
                    <span className="font-mono bg-slate-800/50 px-1 rounded">{loc.location}</span>
                    <span className="text-slate-400 ml-2">({loc.quantity})</span>
                    {loc.lot_number && <span className="text-slate-400 ml-1">Lot: {loc.lot_number}</span>}
                  </div>
                ))}
              </div>
            ) : (
              <span className="text-slate-400">—</span>
            ),
          },
        ]}
      />
    );
  };

  const renderDetailCard = (item: InventoryItem) => (
    <MobileDataCard
      key={item.id}
      title={item.part?.part_number ?? '—'}
      subtitle={item.part?.name}
      badge={renderTypeBadge(item.part?.part_type)}
      fields={[
        {
          label: 'Location',
          value: <span className="font-mono">{item.location}</span>,
        },
        {
          label: 'Lot #',
          value: item.lot_number || '-',
        },
        {
          label: 'Qty',
          value: item.quantity_on_hand,
          className: 'font-medium',
        },
        {
          label: 'Available',
          value: <span className="text-green-600">{item.quantity_available}</span>,
        },
        {
          label: 'Status',
          value: (
            <span className={`px-2 py-1 rounded text-xs ${
              item.status === 'available' ? 'bg-green-500/20 text-emerald-300' :
              item.status === 'quarantine' ? 'bg-yellow-500/20 text-yellow-300' :
              'bg-slate-800/50 text-slate-100'
            }`}>{item.status}</span>
          ),
        },
      ]}
      actions={
        <button
          onClick={(e) => { e.stopPropagation(); openTransfer(item); }}
          className="inline-flex items-center gap-1.5 border border-slate-600 text-slate-200 hover:border-werco-primary hover:text-werco-primary text-sm px-3 py-1 transition-colors"
          aria-label="Transfer inventory"
        >
          <ArrowsRightLeftIcon className="h-4 w-4" />
          Transfer
        </button>
      }
    />
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  if (loadError) {
    return (
      <ErrorState
        message="Could not load inventory data."
        onRetry={loadData}
        className="my-8"
      />
    );
  }

  return (
    <div className="space-y-4">
      {!embedded && (
        <div className="flex justify-between items-center">
          <div>
            <h1 className="text-2xl font-bold text-white">Inventory</h1>
            <p className="text-sm text-slate-400 mt-1">Engineering parts, materials, and supplies in one place</p>
          </div>
          <button onClick={() => setShowReceiveModal(true)} className="btn-primary flex items-center">
            <ArrowDownTrayIcon className="h-5 w-5 mr-2" /> Receive Inventory
          </button>
        </div>
      )}
      {embedded && (
        <div className="flex justify-end">
          <button onClick={() => setShowReceiveModal(true)} className="btn-primary flex items-center">
            <ArrowDownTrayIcon className="h-5 w-5 mr-2" /> Receive Inventory
          </button>
        </div>
      )}

      {/* Summary Stats */}
      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <MiniStat
          icon={CubeIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Unique Items"
          value={filteredSummary.length}
        />
        <MiniStat
          icon={Squares2X2Icon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Total On Hand"
          value={summaryTotals.totalOnHand.toFixed(0)}
        />
        <MiniStat
          icon={ArrowsRightLeftIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Total Available"
          value={summaryTotals.totalAvailable.toFixed(0)}
        />
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg="bg-fd-amber/15"
          iconColor="text-fd-amber"
          label="Low Stock Alerts"
          value={lowStockCount}
          valueColor={lowStockCount > 0 ? 'text-fd-amber' : undefined}
        />
      </MiniStatStrip>

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
                className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-400"
                aria-label="Clear filter"
              >
                <XMarkIcon className="h-4 w-4" aria-hidden="true" />
              </button>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {[
              { id: 'all', label: 'All Inventory' },
              { id: 'parts', label: 'Manufactured & Assemblies' },
              { id: 'materials', label: 'Materials & Supplies' },
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
                    ? 'border-werco-500 bg-werco-500/10 text-werco-700'
                    : 'border-slate-700 text-slate-400 hover:border-werco-300'
                }`}
              >
                {chip.label}
              </button>
            ))}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm text-slate-400">
          <span>Showing</span>
          <span className="px-2 py-1 rounded-full bg-slate-800/50 text-slate-300 font-medium">
            {activeTab === 'details' ? filteredInventory.length : filteredSummary.length}
          </span>
          <span>of</span>
          <span className="px-2 py-1 rounded-full bg-slate-800/50 text-slate-300 font-medium">
            {activeTab === 'details' ? groupInventory.length : groupSummary.length}
          </span>
          <span>items</span>
          {showLowStockOnly ? (
            <span className="ml-2 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-sm text-xs font-medium border border-fd-amber/40 bg-fd-amber/10 text-fd-amber">
              <ExclamationTriangleIcon className="h-3.5 w-3.5" />
              Showing {lowStockCount} low stock
              <button
                type="button"
                onClick={() => {
                  setShowLowStockOnly(false);
                  const nextParams = new URLSearchParams(searchParams);
                  nextParams.delete('filter');
                  setSearchParams(nextParams);
                }}
                className="-mr-0.5 ml-0.5 rounded-sm hover:bg-fd-amber/20"
                aria-label="Clear low stock filter"
              >
                <XMarkIcon className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            </span>
          ) : (
            <button
              type="button"
              onClick={() => {
                setShowLowStockOnly(true);
                const nextParams = new URLSearchParams(searchParams);
                nextParams.set('filter', 'low_stock');
                setSearchParams(nextParams);
              }}
              className="ml-2 px-2.5 py-1 rounded-sm text-xs font-medium border border-fd-line bg-fd-panel text-slate-400 hover:border-fd-line-bright"
            >
              Show Low Stock
            </button>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-slate-700">
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
                  : 'border-transparent text-slate-400 hover:text-slate-300'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Content */}
      <div>
        {activeTab === 'summary' && (
          <DataTable
            columns={summaryColumns}
            data={filteredSummary}
            rowKey={(item) => item.part_id}
            defaultSort={{ key: 'part', dir: 'asc' }}
            pageSize={25}
            csvExport={{ filename: 'inventory-summary' }}
            mobileCards={renderSummaryCard}
            empty={{
              icon: CubeIcon,
              title: 'No inventory on hand',
              description: 'Received parts and materials will appear here once you receive stock.',
              action: { label: 'Receive Inventory', onClick: () => setShowReceiveModal(true) },
            }}
          />
        )}

        {activeTab === 'details' && (
          <DataTable
            columns={detailColumns}
            data={filteredInventory}
            rowKey={(item) => item.id}
            defaultSort={{ key: 'part', dir: 'asc' }}
            pageSize={25}
            csvExport={{ filename: 'inventory-detail' }}
            mobileCards={renderDetailCard}
            empty={{
              icon: CubeIcon,
              title: 'No inventory on hand',
              description: 'Received parts and materials will appear here once you receive stock.',
              action: { label: 'Receive Inventory', onClick: () => setShowReceiveModal(true) },
            }}
          />
        )}
      </div>

      {/* Receive Modal */}
      <Modal open={showReceiveModal} onClose={() => setShowReceiveModal(false)} size="lg" closeOnBackdrop={false}>
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold">Receive Inventory</h3>
              <button onClick={() => setShowReceiveModal(false)} aria-label="Close"><XMarkIcon className="h-6 w-6" aria-hidden="true" /></button>
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
      </Modal>

      {/* Transfer Modal */}
      <Modal
        open={showTransferModal && !!selectedItem}
        onClose={() => setShowTransferModal(false)}
        size="md"
        closeOnBackdrop={false}
      >
        {selectedItem && (
          <>
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold">Transfer Inventory</h3>
              <button onClick={() => setShowTransferModal(false)} aria-label="Close"><XMarkIcon className="h-6 w-6" aria-hidden="true" /></button>
            </div>
            <div className="mb-4 p-3 bg-slate-800 rounded">
              <div className="font-medium">{selectedItem.part?.part_number}</div>
              <div className="text-sm text-slate-400">From: {selectedItem.location}</div>
              <div className="text-sm text-slate-400">Available: {selectedItem.quantity_available}</div>
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
          </>
        )}
      </Modal>
    </div>
  );
}
