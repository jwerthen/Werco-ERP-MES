import React, { useEffect, useMemo, useState } from 'react';
import api from '../services/api';
import { useSearchParams } from 'react-router-dom';
import {
  PlusIcon,
  PencilIcon,
  TrashIcon,
  CheckCircleIcon,
  ArrowPathIcon
} from '@heroicons/react/24/outline';

interface WorkCenter {
  id: number;
  code: string;
  name: string;
  work_center_type: string;
  hourly_rate: number;
}

interface RoutingOperation {
  id: number;
  routing_id: number;
  sequence: number;
  operation_number: string;
  name: string;
  description?: string;
  work_center_id: number;
  work_center?: WorkCenter;
  setup_hours: number;
  run_hours_per_unit: number;
  move_hours: number;
  queue_hours: number;
  is_inspection_point: boolean;
  is_outside_operation: boolean;
  is_active: boolean;
}

interface Routing {
  id: number;
  part_id: number;
  part?: {
    id: number;
    part_number: string;
    name: string;
    part_type: string;
  };
  revision: string;
  description?: string;
  status: string;
  is_active: boolean;
  total_setup_hours: number;
  total_run_hours_per_unit: number;
  total_labor_cost: number;
  operations: RoutingOperation[];
  created_at: string;
}

interface Part {
  id: number;
  part_number: string;
  name: string;
  part_type: string;
}

type BOMLineType = 'component' | 'hardware' | 'consumable' | 'reference';
type BOMItemType = 'make' | 'buy' | 'phantom';

interface BOMItem {
  id: number;
  component_part_id: number;
  item_number: number;
  quantity: number;
  item_type?: BOMItemType;
  line_type?: BOMLineType;
  component_part?: {
    id: number;
    part_number: string;
    name: string;
    revision: string;
    part_type: string;
  };
}

interface BOM {
  id: number;
  part_id: number;
  revision: string;
  status: string;
  description?: string;
  part?: {
    id: number;
    part_number: string;
    name: string;
    part_type: string;
  };
  items: BOMItem[];
}

const lineTypeLabels: Record<string, string> = {
  component: 'Component',
  hardware: 'Hardware',
  consumable: 'Consumable',
  reference: 'Reference',
};

const lineTypeBadge: Record<string, string> = {
  component: 'bg-cyan-100 text-cyan-800',
  hardware: 'bg-amber-100 text-amber-800',
  consumable: 'bg-orange-100 text-orange-800',
  reference: 'bg-gray-100 text-gray-600',
};

const itemTypeBadge: Record<string, string> = {
  make: 'bg-blue-100 text-blue-800',
  buy: 'bg-gray-100 text-gray-700',
  phantom: 'bg-purple-100 text-purple-800',
};

export default function RoutingPage() {
  const [routings, setRoutings] = useState<Routing[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedRouting, setSelectedRouting] = useState<Routing | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showAddOperationModal, setShowAddOperationModal] = useState(false);
  const [editingOperation, setEditingOperation] = useState<RoutingOperation | null>(null);

  const [newRouting, setNewRouting] = useState({ part_id: 0, revision: 'A', description: '' });
  const [newOperation, setNewOperation] = useState({
    sequence: 10,
    name: '',
    description: '',
    work_center_id: 0,
    setup_hours: 0,
    run_hours_per_unit: 0,
    move_hours: 0,
    queue_hours: 0,
    is_inspection_point: false,
    is_outside_operation: false
  });
  const [timeUnits, setTimeUnits] = useState<{ setup: 'hrs' | 'min'; run: 'hrs' | 'min'; move: 'hrs' | 'min'; queue: 'hrs' | 'min' }>({
    setup: 'min',
    run: 'min',
    move: 'min',
    queue: 'min'
  });
  const [searchParams, setSearchParams] = useSearchParams();
  const [assemblySearch, setAssemblySearch] = useState('');
  const [selectedAssemblyId, setSelectedAssemblyId] = useState<number | null>(null);
  const [assemblyBOM, setAssemblyBOM] = useState<BOM | null>(null);
  const [assemblyLoading, setAssemblyLoading] = useState(false);
  const [assemblyError, setAssemblyError] = useState<string | null>(null);
  const [showMissingOnly, setShowMissingOnly] = useState(false);
  const [includeNonComponentLines, setIncludeNonComponentLines] = useState(false);
  const [routingByPartId, setRoutingByPartId] = useState<Record<number, Routing | null>>({});
  const [routingLoadingIds, setRoutingLoadingIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    loadData();
  }, []);

  useEffect(() => {
    const partIdParam = searchParams.get('part_id');
    if (!partIdParam) return;
    const partId = parseInt(partIdParam);
    if (Number.isNaN(partId)) return;

    const existing = parts.find(p => p.id === partId);
    if (existing) {
      setNewRouting({ part_id: partId, revision: 'A', description: '' });
      setShowCreateModal(true);
      return;
    }

    (async () => {
      try {
        const part = await api.getPart(partId);
        if (part) {
          setParts(prev => {
            if (prev.some(p => p.id === partId)) return prev;
            return [...prev, part];
          });
          setNewRouting({ part_id: partId, revision: 'A', description: '' });
          setShowCreateModal(true);
        }
      } catch (err) {
        console.error('Failed to load part from routing param:', err);
      }
    })();
  }, [searchParams, parts]);

  const loadData = async () => {
    try {
      const [routingsRes, partsRes, wcRes] = await Promise.all([
        api.getRoutings(),
        api.getParts({ active_only: true }),
        api.getWorkCenters()
      ]);
      setRoutings(routingsRes);
      setParts(partsRes);
      setWorkCenters(wcRes);

    } catch (err) {
      console.error('Failed to load data:', err);
    } finally {
      setLoading(false);
    }
  };

  const loadRouting = async (id: number) => {
    try {
      const routing = await api.getRouting(id);
      setSelectedRouting(routing);
      return routing;
    } catch (err) {
      console.error('Failed to load routing:', err);
    }
    return null;
  };

  const ensureRoutingStatuses = async (partIds: number[]) => {
    const uniqueIds = Array.from(new Set(partIds.filter(Boolean)));
    const missing = uniqueIds.filter((id) => routingByPartId[id] === undefined);
    if (missing.length === 0) return;

    setRoutingLoadingIds((prev) => new Set([...prev, ...missing]));

    const results: Array<[number, Routing | null]> = [];
    const batchSize = 10;

    for (let i = 0; i < missing.length; i += batchSize) {
      const batch = missing.slice(i, i + batchSize);
      const batchResults = await Promise.all(
        batch.map(async (id) => {
          try {
            const routing = await api.getRoutingByPart(id);
            return [id, routing || null] as [number, Routing | null];
          } catch (err) {
            console.error('Failed to load routing by part:', err);
            return [id, null] as [number, Routing | null];
          }
        })
      );
      results.push(...batchResults);
    }

    setRoutingByPartId((prev) => {
      const next = { ...prev };
      results.forEach(([id, routing]) => {
        next[id] = routing;
      });
      return next;
    });

    setRoutingLoadingIds((prev) => {
      const next = new Set(prev);
      missing.forEach((id) => next.delete(id));
      return next;
    });
  };

  const loadAssemblyBOM = async (partId: number) => {
    setAssemblyLoading(true);
    setAssemblyError(null);
    try {
      const bom = await api.getBOMByPart(partId);
      setAssemblyBOM(bom);
      const routableIds = (bom.items || [])
        .filter((item: BOMItem) => (item.line_type || 'component') === 'component')
        .map((item: BOMItem) => item.component_part_id);
      await ensureRoutingStatuses(routableIds);
    } catch (err: any) {
      if (err?.response?.status === 404) {
        setAssemblyBOM(null);
        setAssemblyError('No BOM found for this assembly.');
      } else {
        console.error('Failed to load BOM:', err);
        setAssemblyBOM(null);
        setAssemblyError('Failed to load BOM for this assembly.');
      }
    } finally {
      setAssemblyLoading(false);
    }
  };

  const openCreateRoutingForPart = async (partId: number, partLabel?: string) => {
    if (!parts.some((p) => p.id === partId)) {
      try {
        const part = await api.getPart(partId);
        setParts((prev) => (prev.some((p) => p.id === partId) ? prev : [...prev, part]));
      } catch (err) {
        console.error('Failed to load part for routing:', err);
      }
    }
    setNewRouting({
      part_id: partId,
      revision: 'A',
      description: partLabel ? `Routing for ${partLabel}` : ''
    });
    setShowCreateModal(true);
  };

  const handleAssemblySelect = (value: string) => {
    const nextId = parseInt(value, 10);
    if (!value || Number.isNaN(nextId)) {
      setSelectedAssemblyId(null);
      setAssemblyBOM(null);
      setAssemblyError(null);
      return;
    }
    setSelectedAssemblyId(nextId);
    loadAssemblyBOM(nextId);
  };

  const handleCreateRouting = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const created = await api.createRouting(newRouting);
      setRoutings([created, ...routings]);
      setSelectedRouting(created);
      setRoutingByPartId((prev) => ({ ...prev, [created.part_id]: created }));
      setShowCreateModal(false);
      setNewRouting({ part_id: 0, revision: 'A', description: '' });
      const nextParams = new URLSearchParams(searchParams);
      nextParams.delete('part_id');
      setSearchParams(nextParams);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create routing');
    }
  };

  const handleAddOperation = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedRouting) return;

    try {
      if (editingOperation) {
        await api.updateRoutingOperation(selectedRouting.id, editingOperation.id, newOperation);
      } else {
        await api.addRoutingOperation(selectedRouting.id, newOperation);
      }
      await loadRouting(selectedRouting.id);
      setShowAddOperationModal(false);
      setEditingOperation(null);
      resetOperationForm();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to save operation');
    }
  };

  const handleDeleteOperation = async (operationId: number) => {
    if (!selectedRouting || !window.confirm('Delete this operation?')) return;

    try {
      await api.deleteRoutingOperation(selectedRouting.id, operationId);
      await loadRouting(selectedRouting.id);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to delete operation');
    }
  };

  const handleReleaseRouting = async () => {
    if (!selectedRouting) return;

    try {
      await api.releaseRouting(selectedRouting.id);
      const updated = await loadRouting(selectedRouting.id);
      if (updated) {
        setRoutingByPartId((prev) => ({ ...prev, [updated.part_id]: updated }));
      }
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to release routing');
    }
  };

  const handleDeleteRouting = async (routing: Routing) => {
    const message = routing.status === 'draft' 
      ? `Delete routing for ${routing.part?.part_number}? This will permanently delete it.`
      : `Deactivate routing for ${routing.part?.part_number}? It will be marked as obsolete.`;
    
    if (!window.confirm(message)) return;

    try {
      await api.deleteRouting(routing.id);
      setRoutingByPartId((prev) => {
        const next = { ...prev };
        if (routing.part_id) {
          next[routing.part_id] = null;
        }
        return next;
      });
      if (selectedRouting?.id === routing.id) {
        setSelectedRouting(null);
      }
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to delete routing');
    }
  };

  const openEditOperation = (op: RoutingOperation) => {
    setEditingOperation(op);
    setNewOperation({
      sequence: op.sequence,
      name: op.name,
      description: op.description || '',
      work_center_id: op.work_center_id,
      setup_hours: op.setup_hours,
      run_hours_per_unit: op.run_hours_per_unit,
      move_hours: op.move_hours,
      queue_hours: op.queue_hours,
      is_inspection_point: op.is_inspection_point,
      is_outside_operation: op.is_outside_operation
    });
    setShowAddOperationModal(true);
  };

  const resetOperationForm = () => {
    const nextSeq = selectedRouting?.operations.length 
      ? Math.max(...selectedRouting.operations.map(o => o.sequence)) + 10 
      : 10;
    setNewOperation({
      sequence: nextSeq,
      name: '',
      description: '',
      work_center_id: 0,
      setup_hours: 0,
      run_hours_per_unit: 0,
      move_hours: 0,
      queue_hours: 0,
      is_inspection_point: false,
      is_outside_operation: false
    });
  };

  const openAddOperationModal = () => {
    setEditingOperation(null);
    resetOperationForm();
    setShowAddOperationModal(true);
  };

  const formatHours = (hours: number) => {
    if (hours < 1) {
      return `${Math.round(hours * 60)} min`;
    }
    return `${hours.toFixed(2)} hr`;
  };

  const filteredAssemblies = useMemo(() => {
    const search = assemblySearch.trim().toLowerCase();
    const base = parts.filter((part) => ['assembly', 'manufactured'].includes(part.part_type));
    if (!search) {
      return base.sort((a, b) => a.part_number.localeCompare(b.part_number));
    }
    return base
      .filter((part) =>
        part.part_number.toLowerCase().includes(search) ||
        part.name.toLowerCase().includes(search)
      )
      .sort((a, b) => a.part_number.localeCompare(b.part_number));
  }, [assemblySearch, parts]);

  const routablePartIds = useMemo(() => {
    if (!assemblyBOM) return new Set<number>();
    const ids = new Set<number>();
    (assemblyBOM.items || []).forEach((item) => {
      if ((item.line_type || 'component') === 'component') {
        ids.add(item.component_part_id);
      }
    });
    return ids;
  }, [assemblyBOM]);

  const assemblyItems = useMemo(() => {
    if (!assemblyBOM) return [];
    const items = assemblyBOM.items || [];
    let filtered = items;
    if (!includeNonComponentLines) {
      filtered = filtered.filter((item) => (item.line_type || 'component') === 'component');
    }
    if (showMissingOnly) {
      filtered = filtered.filter((item) => {
        if ((item.line_type || 'component') !== 'component') return false;
        const status = routingByPartId[item.component_part_id];
        return status === null;
      });
    }
    return filtered;
  }, [assemblyBOM, includeNonComponentLines, showMissingOnly, routingByPartId]);

  const assemblySummary = useMemo(() => {
    if (!assemblyBOM) return null;
    const componentItems = (assemblyBOM.items || []).filter(
      (item) => (item.line_type || 'component') === 'component'
    );
    const total = componentItems.length;
    const withRouting = componentItems.filter((item) => routingByPartId[item.component_part_id]).length;
    const missing = componentItems.filter((item) => routingByPartId[item.component_part_id] === null).length;
    const checking = componentItems.filter((item) => routingByPartId[item.component_part_id] === undefined).length;
    return { total, withRouting, missing, checking };
  }, [assemblyBOM, routingByPartId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6" data-tour="eng-routing">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-gray-900">Operations Routing</h1>
        <button onClick={() => setShowCreateModal(true)} className="btn-primary flex items-center">
          <PlusIcon className="h-5 w-5 mr-2" />
          New Routing
        </button>
      </div>

      <div className="card">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold">Assembly Components</h2>
            <p className="text-sm text-gray-500">
              Select an assembly to review component routings and create missing ones.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-4 text-sm text-gray-600">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={showMissingOnly}
                onChange={(e) => setShowMissingOnly(e.target.checked)}
                className="rounded border-gray-300 text-cyan-600 focus:ring-cyan-500"
              />
              Missing only
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={includeNonComponentLines}
                onChange={(e) => setIncludeNonComponentLines(e.target.checked)}
                className="rounded border-gray-300 text-cyan-600 focus:ring-cyan-500"
              />
              Include hardware/consumables
            </label>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-4">
          <div className="lg:col-span-2 space-y-2">
            <label className="label">Assembly</label>
            <input
              type="text"
              value={assemblySearch}
              onChange={(e) => setAssemblySearch(e.target.value)}
              placeholder="Filter assemblies..."
              className="input"
            />
            <select
              value={selectedAssemblyId || 0}
              onChange={(e) => handleAssemblySelect(e.target.value)}
              className="input"
            >
              <option value={0}>Select an assembly...</option>
              {filteredAssemblies.map((part) => (
                <option key={part.id} value={part.id}>
                  {part.part_number} - {part.name}
                </option>
              ))}
            </select>
          </div>
          <div className="rounded-lg border border-gray-200 bg-gray-50 p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wide mb-2">Summary</div>
            {assemblySummary ? (
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <div className="text-gray-500">Components</div>
                  <div className="font-semibold">{assemblySummary.total}</div>
                </div>
                <div>
                  <div className="text-gray-500">With Routing</div>
                  <div className="font-semibold text-green-700">{assemblySummary.withRouting}</div>
                </div>
                <div>
                  <div className="text-gray-500">Missing</div>
                  <div className="font-semibold text-amber-700">{assemblySummary.missing}</div>
                </div>
                <div>
                  <div className="text-gray-500">Checking</div>
                  <div className="font-semibold text-gray-600">{assemblySummary.checking}</div>
                </div>
              </div>
            ) : (
              <div className="text-sm text-gray-500">Select an assembly to view routing status.</div>
            )}
          </div>
        </div>

        {assemblyLoading && (
          <div className="flex items-center gap-2 text-sm text-gray-500">
            <div className="h-4 w-4 animate-spin rounded-full border-2 border-gray-300 border-t-werco-primary"></div>
            Loading BOM...
          </div>
        )}

        {assemblyError && (
          <div className="text-sm text-amber-600 bg-amber-50 px-3 py-2 rounded-lg">
            {assemblyError}
          </div>
        )}

        {assemblyBOM && (
          <div className="mt-4 overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Item #</th>
                  <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                  <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Line Type</th>
                  <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Make/Buy</th>
                  <th className="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
                  <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Routing</th>
                  <th className="px-3 py-2 text-right text-xs font-medium text-gray-500 uppercase">Action</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {assemblyItems.map((item) => {
                  const part = item.component_part;
                  const lineType = item.line_type || 'component';
                  const itemType = item.item_type || 'buy';
                  const isRoutable = lineType === 'component' && itemType !== 'buy';
                  const routing = routingByPartId[item.component_part_id];
                  const loadingRouting = routingLoadingIds.has(item.component_part_id);
                  return (
                    <tr key={item.id} className="hover:bg-gray-50">
                      <td className="px-3 py-2 text-sm font-medium">
                        {item.item_number}
                      </td>
                      <td className="px-3 py-2 text-sm">
                        <div className="font-medium text-werco-primary">
                          {part?.part_number || `Part #${item.component_part_id}`}
                        </div>
                        <div className="text-xs text-gray-500">{part?.name || '-'}</div>
                      </td>
                      <td className="px-3 py-2 text-sm">
                        <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${lineTypeBadge[lineType] || 'bg-gray-100 text-gray-600'}`}>
                          {lineTypeLabels[lineType] || lineType}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-sm">
                        <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${itemTypeBadge[itemType] || 'bg-gray-100 text-gray-600'}`}>
                          {itemType}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-sm text-right">{item.quantity}</td>
                      <td className="px-3 py-2 text-sm">
                        {loadingRouting && isRoutable && (
                          <span className="text-gray-400">Checking...</span>
                        )}
                        {!loadingRouting && isRoutable && routing && (
                          <span className="text-green-700 text-sm font-medium">
                            {routing.status} (Rev {routing.revision})
                          </span>
                        )}
                        {!loadingRouting && isRoutable && routing === null && (
                          <span className="text-amber-700 text-sm font-medium">Missing</span>
                        )}
                        {!loadingRouting && isRoutable && routing === undefined && (
                          <span className="text-gray-400 text-sm">Pending</span>
                        )}
                        {!isRoutable && (
                          <span className="text-gray-400 text-sm">Not routable</span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-right">
                        {isRoutable && routing && (
                          <button
                            type="button"
                            onClick={() => loadRouting(routing.id)}
                            className="text-werco-primary hover:underline text-sm"
                          >
                            View
                          </button>
                        )}
                        {isRoutable && routing === null && (
                          <button
                            type="button"
                            onClick={() => openCreateRoutingForPart(item.component_part_id, part?.part_number || part?.name)}
                            className="text-werco-primary hover:underline text-sm"
                          >
                            Create
                          </button>
                        )}
                        {isRoutable && routing === undefined && (
                          <span className="text-xs text-gray-400">Checking...</span>
                        )}
                        {!isRoutable && (
                          <span className="text-xs text-gray-400">-</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {assemblyItems.length === 0 && (
              <div className="text-sm text-gray-500 py-4 text-center">
                No matching components to display.
              </div>
            )}
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Routings List */}
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">Routings</h2>
          <div className="space-y-2 max-h-[600px] overflow-y-auto">
            {routings.map((routing) => (
              <div
                key={routing.id}
                onClick={() => loadRouting(routing.id)}
                className={`p-3 rounded-lg border cursor-pointer transition-colors ${
                  selectedRouting?.id === routing.id
                    ? 'border-werco-primary bg-blue-50'
                    : 'border-gray-200 hover:border-gray-300'
                }`}
              >
                <div className="flex justify-between items-start">
                  <div className="flex-1">
                    <div className="font-medium">{routing.part?.part_number}</div>
                    <div className="text-sm text-gray-500">{routing.part?.name}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`text-xs px-2 py-1 rounded ${
                      routing.status === 'released' ? 'bg-green-100 text-green-800' :
                      routing.status === 'draft' ? 'bg-yellow-100 text-yellow-800' :
                      'bg-gray-100 text-gray-800'
                    }`}>
                      {routing.status}
                    </span>
                    <button
                      onClick={(e) => { e.stopPropagation(); handleDeleteRouting(routing); }}
                      className="text-gray-400 hover:text-red-600 p-1"
                      title={routing.status === 'draft' ? 'Delete' : 'Deactivate'}
                    >
                      <TrashIcon className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                <div className="text-xs text-gray-400 mt-1">
                  Rev {routing.revision} | {routing.operations?.length || 0} operations
                </div>
              </div>
            ))}
            {routings.length === 0 && (
              <p className="text-gray-500 text-center py-4">No routings created yet</p>
            )}
          </div>
        </div>

        {/* Routing Detail */}
        <div className="card lg:col-span-2">
          {selectedRouting ? (
            <>
              <div className="flex justify-between items-start mb-4">
                <div>
                  <h2 className="text-lg font-semibold">{selectedRouting.part?.part_number}</h2>
                  <p className="text-gray-500">{selectedRouting.part?.name}</p>
                  <p className="text-sm text-gray-400">Revision {selectedRouting.revision}</p>
                </div>
                <div className="flex gap-2">
                  {selectedRouting.status === 'draft' && (
                    <>
                      <button onClick={openAddOperationModal} className="btn-secondary flex items-center">
                        <PlusIcon className="h-4 w-4 mr-1" />
                        Add Operation
                      </button>
                      <button onClick={handleReleaseRouting} className="btn-success">
                        Release
                      </button>
                    </>
                  )}
                </div>
              </div>

              {/* Totals Summary */}
              <div className="grid grid-cols-4 gap-4 mb-4">
                <div className="bg-gray-50 rounded-lg p-3">
                  <div className="text-sm text-gray-500">Total Setup</div>
                  <div className="text-lg font-semibold">{formatHours(selectedRouting.total_setup_hours)}</div>
                </div>
                <div className="bg-gray-50 rounded-lg p-3">
                  <div className="text-sm text-gray-500">Run Time/Unit</div>
                  <div className="text-lg font-semibold">{formatHours(selectedRouting.total_run_hours_per_unit)}</div>
                </div>
                <div className="bg-gray-50 rounded-lg p-3">
                  <div className="text-sm text-gray-500">Labor Cost</div>
                  <div className="text-lg font-semibold">${selectedRouting.total_labor_cost.toFixed(2)}</div>
                </div>
                <div className="bg-gray-50 rounded-lg p-3">
                  <div className="text-sm text-gray-500">Operations</div>
                  <div className="text-lg font-semibold">{selectedRouting.operations.length}</div>
                </div>
              </div>

              {/* Operations Table */}
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Op #</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Operation</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Work Center</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Setup</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Run/Unit</th>
                      <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Inspect</th>
                      {selectedRouting.status === 'draft' && (
                        <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
                      )}
                    </tr>
                  </thead>
                  <tbody className="bg-white divide-y divide-gray-200">
                    {selectedRouting.operations
                      .sort((a, b) => a.sequence - b.sequence)
                      .map((op) => (
                        <tr key={op.id} className="hover:bg-gray-50">
                          <td className="px-4 py-3 font-medium">{op.operation_number}</td>
                          <td className="px-4 py-3">
                            <div>{op.name}</div>
                            {op.description && (
                              <div className="text-xs text-gray-400">{op.description}</div>
                            )}
                          </td>
                          <td className="px-4 py-3">
                            <div className="font-medium">{op.work_center?.code}</div>
                            <div className="text-xs text-gray-400">{op.work_center?.name}</div>
                          </td>
                          <td className="px-4 py-3 text-right">{formatHours(op.setup_hours)}</td>
                          <td className="px-4 py-3 text-right">{formatHours(op.run_hours_per_unit)}</td>
                          <td className="px-4 py-3 text-center">
                            {op.is_inspection_point && (
                              <CheckCircleIcon className="h-5 w-5 text-blue-500 mx-auto" />
                            )}
                          </td>
                          {selectedRouting.status === 'draft' && (
                            <td className="px-4 py-3 text-center">
                              <button
                                onClick={() => openEditOperation(op)}
                                className="text-gray-400 hover:text-werco-primary mr-2"
                              >
                                <PencilIcon className="h-5 w-5" />
                              </button>
                              <button
                                onClick={() => handleDeleteOperation(op.id)}
                                className="text-gray-400 hover:text-red-500"
                              >
                                <TrashIcon className="h-5 w-5" />
                              </button>
                            </td>
                          )}
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>

              {selectedRouting.operations.length === 0 && (
                <p className="text-gray-500 text-center py-8">No operations defined yet</p>
              )}
            </>
          ) : (
            <div className="text-center py-12 text-gray-500">
              <ArrowPathIcon className="h-12 w-12 mx-auto mb-4 text-gray-300" />
              <p>Select a routing to view operations</p>
            </div>
          )}
        </div>
      </div>

      {/* Create Routing Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Create New Routing</h3>
            <form onSubmit={handleCreateRouting} className="space-y-4">
              <div>
                <label className="label">Part</label>
                <select
                  value={newRouting.part_id}
                  onChange={(e) => setNewRouting({ ...newRouting, part_id: parseInt(e.target.value) })}
                  className="input"
                  required
                >
                  <option value={0}>Select a part...</option>
                  {parts
                    .filter(p => ['assembly', 'manufactured'].includes(p.part_type) || routablePartIds.has(p.id) || p.id === newRouting.part_id)
                    .map(part => (
                      <option key={part.id} value={part.id}>
                        {part.part_number} - {part.name}
                      </option>
                    ))}
                </select>
              </div>
              <div>
                <label className="label">Revision</label>
                <input
                  type="text"
                  value={newRouting.revision}
                  onChange={(e) => setNewRouting({ ...newRouting, revision: e.target.value })}
                  className="input"
                  required
                />
              </div>
              <div>
                <label className="label">Description</label>
                <textarea
                  value={newRouting.description}
                  onChange={(e) => setNewRouting({ ...newRouting, description: e.target.value })}
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

      {/* Add/Edit Operation Modal */}
      {showAddOperationModal && selectedRouting && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" onClick={() => setShowAddOperationModal(false)}>
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-lg font-semibold mb-4">
              {editingOperation ? 'Edit Operation' : 'Add Operation'}
            </h3>
            <form onSubmit={handleAddOperation} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Sequence #</label>
                  <input
                    type="number"
                    value={newOperation.sequence}
                    onChange={(e) => setNewOperation({ ...newOperation, sequence: parseInt(e.target.value) })}
                    className="input"
                    step={10}
                    required
                  />
                </div>
                <div>
                  <label className="label">Work Center</label>
                  <select
                    value={newOperation.work_center_id}
                    onChange={(e) => setNewOperation({ ...newOperation, work_center_id: parseInt(e.target.value) })}
                    className="input"
                    required
                  >
                    <option value={0}>Select...</option>
                    {workCenters.map(wc => (
                      <option key={wc.id} value={wc.id}>
                        {wc.code} - {wc.name}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div>
                <label className="label">Operation Name</label>
                <input
                  type="text"
                  value={newOperation.name}
                  onChange={(e) => setNewOperation({ ...newOperation, name: e.target.value })}
                  className="input"
                  placeholder="e.g., Cut to size, Weld assembly, Paint"
                  required
                />
              </div>
              <div>
                <label className="label">Description</label>
                <textarea
                  value={newOperation.description}
                  onChange={(e) => setNewOperation({ ...newOperation, description: e.target.value })}
                  className="input"
                  rows={2}
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Setup Time</label>
                  <div className="flex gap-2">
                    <input
                      type="number"
                      value={timeUnits.setup === 'min' ? Math.round(newOperation.setup_hours * 60 * 100) / 100 : newOperation.setup_hours}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setNewOperation({ ...newOperation, setup_hours: timeUnits.setup === 'min' ? Math.round(val / 60 * 10000) / 10000 : val });
                      }}
                      className="input flex-1"
                      step={timeUnits.setup === 'min' ? 1 : 0.01}
                      min={0}
                    />
                    <select
                      value={timeUnits.setup}
                      onChange={(e) => setTimeUnits({ ...timeUnits, setup: e.target.value as 'hrs' | 'min' })}
                      className="border border-gray-300 rounded-lg px-3 py-2 w-20 bg-white cursor-pointer focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    >
                      <option value="min">min</option>
                      <option value="hrs">hrs</option>
                    </select>
                  </div>
                </div>
                <div>
                  <label className="label">Run Time/Unit</label>
                  <div className="flex gap-2">
                    <input
                      type="number"
                      value={timeUnits.run === 'min' ? Math.round(newOperation.run_hours_per_unit * 60 * 100) / 100 : newOperation.run_hours_per_unit}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setNewOperation({ ...newOperation, run_hours_per_unit: timeUnits.run === 'min' ? Math.round(val / 60 * 10000) / 10000 : val });
                      }}
                      className="input flex-1"
                      step={timeUnits.run === 'min' ? 0.1 : 0.001}
                      min={0}
                    />
                    <select
                      value={timeUnits.run}
                      onChange={(e) => setTimeUnits({ ...timeUnits, run: e.target.value as 'hrs' | 'min' })}
                      className="border border-gray-300 rounded-lg px-3 py-2 w-20 bg-white cursor-pointer focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    >
                      <option value="min">min</option>
                      <option value="hrs">hrs</option>
                    </select>
                  </div>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Move Time</label>
                  <div className="flex gap-2">
                    <input
                      type="number"
                      value={timeUnits.move === 'min' ? Math.round(newOperation.move_hours * 60 * 100) / 100 : newOperation.move_hours}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setNewOperation({ ...newOperation, move_hours: timeUnits.move === 'min' ? Math.round(val / 60 * 10000) / 10000 : val });
                      }}
                      className="input flex-1"
                      step={timeUnits.move === 'min' ? 1 : 0.01}
                      min={0}
                    />
                    <select
                      value={timeUnits.move}
                      onChange={(e) => setTimeUnits({ ...timeUnits, move: e.target.value as 'hrs' | 'min' })}
                      className="border border-gray-300 rounded-lg px-3 py-2 w-20 bg-white cursor-pointer focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    >
                      <option value="min">min</option>
                      <option value="hrs">hrs</option>
                    </select>
                  </div>
                </div>
                <div>
                  <label className="label">Queue Time</label>
                  <div className="flex gap-2">
                    <input
                      type="number"
                      value={timeUnits.queue === 'min' ? Math.round(newOperation.queue_hours * 60 * 100) / 100 : newOperation.queue_hours}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setNewOperation({ ...newOperation, queue_hours: timeUnits.queue === 'min' ? Math.round(val / 60 * 10000) / 10000 : val });
                      }}
                      className="input flex-1"
                      step={timeUnits.queue === 'min' ? 1 : 0.01}
                      min={0}
                    />
                    <select
                      value={timeUnits.queue}
                      onChange={(e) => setTimeUnits({ ...timeUnits, queue: e.target.value as 'hrs' | 'min' })}
                      className="border border-gray-300 rounded-lg px-3 py-2 w-20 bg-white cursor-pointer focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    >
                      <option value="min">min</option>
                      <option value="hrs">hrs</option>
                    </select>
                  </div>
                </div>
              </div>
              <div className="flex gap-6">
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={newOperation.is_inspection_point}
                    onChange={(e) => setNewOperation({ ...newOperation, is_inspection_point: e.target.checked })}
                    className="mr-2"
                  />
                  <span className="text-sm">Inspection Point</span>
                </label>
                <label className="flex items-center">
                  <input
                    type="checkbox"
                    checked={newOperation.is_outside_operation}
                    onChange={(e) => setNewOperation({ ...newOperation, is_outside_operation: e.target.checked })}
                    className="mr-2"
                  />
                  <span className="text-sm">Outside Operation</span>
                </label>
              </div>
              <div className="flex justify-end gap-3 pt-4">
                <button
                  type="button"
                  onClick={() => {
                    setShowAddOperationModal(false);
                    setEditingOperation(null);
                  }}
                  className="btn-secondary"
                >
                  Cancel
                </button>
                <button type="submit" className="btn-primary">
                  {editingOperation ? 'Update' : 'Add'} Operation
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
