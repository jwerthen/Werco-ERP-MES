import React, { useEffect, useMemo, useState, useRef } from 'react';
import api from '../services/api';
import { useSearchParams } from 'react-router-dom';
import {
  PlusIcon,
  PencilIcon,
  TrashIcon,
  CheckCircleIcon,
  ArrowPathIcon,
  DocumentArrowUpIcon,
  SparklesIcon,
  ExclamationTriangleIcon,
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

interface RoutingPartOption {
  id: number;
  part_number: string;
  name: string;
  part_type?: string;
}

interface DrawingExtractionInfo {
  material?: string;
  thickness?: string;
  finish?: string;
  tolerances_noted: boolean;
  weld_required: boolean;
  assembly_required: boolean;
  flat_length?: number;
  flat_width?: number;
  cut_length?: number;
  hole_count?: number;
  bend_count?: number;
}

interface ProposedOperation {
  sequence: number;
  operation_name: string;
  description?: string;
  work_center_type: string;
  work_center_id?: number;
  work_center_name?: string;
  setup_hours: number;
  run_hours_per_unit: number;
  is_inspection_point: boolean;
  is_outside_operation: boolean;
  tooling_requirements?: string;
  work_instructions?: string;
  confidence: string;
}

interface GenerationResult {
  part_id: number;
  part_number: string;
  part_name: string;
  drawing_info: DrawingExtractionInfo;
  proposed_operations: ProposedOperation[];
  extraction_confidence: string;
  file_type: string;
  warnings: string[];
  existing_routing_warning?: string;
}

const confidenceBadge: Record<string, string> = {
  high: 'bg-green-500/20 text-green-300',
  medium: 'bg-yellow-500/20 text-yellow-300',
  low: 'bg-red-500/20 text-red-300',
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
  const [forcedRoutingPart, setForcedRoutingPart] = useState<RoutingPartOption | null>(null);
  const [routingPartSearch, setRoutingPartSearch] = useState('');
  const [routingPartOpen, setRoutingPartOpen] = useState(false);

  // Generate from Drawing state
  const [showGenerateModal, setShowGenerateModal] = useState(false);
  const [generatePartId, setGeneratePartId] = useState<number>(0);
  const [generatePartSearch, setGeneratePartSearch] = useState('');
  const [generatePartOpen, setGeneratePartOpen] = useState(false);
  const [generateFile, setGenerateFile] = useState<File | null>(null);
  const [generating, setGenerating] = useState(false);
  const [generationResult, setGenerationResult] = useState<GenerationResult | null>(null);
  const [editedOperations, setEditedOperations] = useState<ProposedOperation[]>([]);
  const [creatingFromGeneration, setCreatingFromGeneration] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    loadData();
  }, []);

  useEffect(() => {
    const partIdParam = searchParams.get('part_id');
    if (!partIdParam) return;
    const partId = parseInt(partIdParam);
    if (Number.isNaN(partId)) return;

    const existing = parts.find(p => p.id === partId);
    if (newRouting.part_id !== partId) {
      setNewRouting({ part_id: partId, revision: 'A', description: '' });
      setShowCreateModal(true);
      setForcedRoutingPart({
        id: partId,
        part_number: existing?.part_number || `Part #${partId}`,
        name: existing?.name || 'Loading...'
      });
    }
    if (existing) {
      setForcedRoutingPart({ id: existing.id, part_number: existing.part_number, name: existing.name });
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
          setForcedRoutingPart({ id: part.id, part_number: part.part_number, name: part.name });
        }
      } catch (err) {
        console.error('Failed to load part from routing param:', err);
      }
    })();
  }, [searchParams, parts, newRouting.part_id]);

  const loadData = async () => {
    try {
      const [routingsRes, partsRes, wcRes] = await Promise.all([
        api.getRoutings({ include_bom_components: false }),
        api.getParts({ active_only: true, include_bom_components: false }),
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

  const handleCreateRouting = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newRouting.part_id) {
      alert('Select a part before creating a routing.');
      return;
    }
    try {
      const created = await api.createRouting(newRouting);
      setRoutings([created, ...routings]);
      setSelectedRouting(created);
      setShowCreateModal(false);
      setNewRouting({ part_id: 0, revision: 'A', description: '' });
      setForcedRoutingPart(null);
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
      await loadRouting(selectedRouting.id);
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

  // Generate from Drawing handlers
  const openGenerateModal = () => {
    setShowGenerateModal(true);
    setGeneratePartId(0);
    setGeneratePartSearch('');
    setGenerateFile(null);
    setGenerationResult(null);
    setEditedOperations([]);
  };

  const handleAnalyzeDrawing = async () => {
    if (!generateFile || !generatePartId) return;
    setGenerating(true);
    try {
      const result: GenerationResult = await api.generateRoutingFromDrawing(generateFile, generatePartId);
      setGenerationResult(result);
      setEditedOperations(result.proposed_operations);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to analyze drawing');
    } finally {
      setGenerating(false);
    }
  };

  const handleCreateFromGeneration = async () => {
    if (!generationResult || editedOperations.length === 0) return;
    setCreatingFromGeneration(true);
    try {
      const operations = editedOperations
        .filter((op) => op.work_center_id)
        .map((op) => ({
          sequence: op.sequence,
          name: op.operation_name,
          description: op.description || '',
          work_center_id: op.work_center_id!,
          setup_hours: op.setup_hours,
          run_hours_per_unit: op.run_hours_per_unit,
          is_inspection_point: op.is_inspection_point,
          is_outside_operation: op.is_outside_operation,
          tooling_requirements: op.tooling_requirements || undefined,
          work_instructions: op.work_instructions || undefined,
          move_hours: 0,
          queue_hours: 0,
        }));
      if (operations.length === 0) {
        alert('All operations need a work center assigned before creating the routing.');
        return;
      }
      const created = await api.createRoutingFromGeneration({
        part_id: generationResult.part_id,
        revision: 'A',
        description: `Auto-generated from ${generationResult.file_type.toUpperCase()} drawing`,
        operations,
      });
      setRoutings([created, ...routings]);
      setSelectedRouting(created);
      setShowGenerateModal(false);
      setGenerationResult(null);
      setEditedOperations([]);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create routing');
    } finally {
      setCreatingFromGeneration(false);
    }
  };

  const updateEditedOp = (index: number, field: string, value: any) => {
    setEditedOperations((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], [field]: value };
      return next;
    });
  };

  const removeEditedOp = (index: number) => {
    setEditedOperations((prev) => prev.filter((_, i) => i !== index));
  };

  const filteredGenerateParts = useMemo(() => {
    const search = generatePartSearch.trim().toLowerCase();
    const base = parts
      .filter((p) => ['assembly', 'manufactured'].includes(p.part_type))
      .sort((a, b) => a.part_number.localeCompare(b.part_number));
    if (!search) return base.slice(0, 50);
    return base
      .filter((p) => p.part_number.toLowerCase().includes(search) || p.name.toLowerCase().includes(search))
      .slice(0, 50);
  }, [generatePartSearch, parts]);

  const formatHours = (hours: number) => {
    if (hours < 1) {
      return `${Math.round(hours * 60)} min`;
    }
    return `${hours.toFixed(2)} hr`;
  };

  const routingPartOptions = useMemo(() => {
    const base = parts.filter(
      (part) =>
        ['assembly', 'manufactured'].includes(part.part_type) ||
        part.id === newRouting.part_id
    );
    if (forcedRoutingPart && !base.some((part) => part.id === forcedRoutingPart.id)) {
      return [
        { ...forcedRoutingPart, part_type: 'manufactured' } as Part,
        ...base
      ];
    }
    return base;
  }, [parts, forcedRoutingPart, newRouting.part_id]);

  const selectedRoutingPart = useMemo(() => {
    if (!newRouting.part_id) return forcedRoutingPart;
    return (
      routingPartOptions.find((part) => part.id === newRouting.part_id) ||
      forcedRoutingPart ||
      null
    );
  }, [newRouting.part_id, routingPartOptions, forcedRoutingPart]);

  const filteredRoutingParts = useMemo(() => {
    const search = routingPartSearch.trim().toLowerCase();
    const base = [...routingPartOptions].sort((a, b) => a.part_number.localeCompare(b.part_number));
    if (!search) {
      return base.slice(0, 50);
    }
    return base
      .filter((part) =>
        part.part_number.toLowerCase().includes(search) ||
        part.name.toLowerCase().includes(search)
      )
      .slice(0, 50);
  }, [routingPartOptions, routingPartSearch]);

  useEffect(() => {
    if (!showCreateModal) return;
    if (selectedRoutingPart) {
      setRoutingPartSearch(`${selectedRoutingPart.part_number} - ${selectedRoutingPart.name}`);
    } else {
      setRoutingPartSearch('');
    }
  }, [showCreateModal, selectedRoutingPart]);

  const handleSelectRoutingPart = (part: RoutingPartOption) => {
    setNewRouting({ ...newRouting, part_id: part.id });
    setRoutingPartSearch(`${part.part_number} - ${part.name}`);
    setRoutingPartOpen(false);
    if (forcedRoutingPart && forcedRoutingPart.id !== part.id) {
      setForcedRoutingPart(null);
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
    <div className="space-y-6" data-tour="eng-routing">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Operations Routing</h1>
        <div className="flex gap-3">
          <button
            onClick={openGenerateModal}
            className="btn-secondary flex items-center"
          >
            <SparklesIcon className="h-5 w-5 mr-2" />
            Generate from Drawing
          </button>
          <button
            onClick={() => {
              setNewRouting({ part_id: 0, revision: 'A', description: '' });
              setForcedRoutingPart(null);
              setShowCreateModal(true);
            }}
            className="btn-primary flex items-center"
          >
            <PlusIcon className="h-5 w-5 mr-2" />
            New Routing
          </button>
        </div>
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
                    ? 'border-werco-primary bg-blue-500/10'
                    : 'border-slate-700 hover:border-slate-600'
                }`}
              >
                <div className="flex justify-between items-start">
                  <div className="flex-1">
                    <div className="font-medium">{routing.part?.part_number}</div>
                    <div className="text-sm text-slate-400">{routing.part?.name}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`text-xs px-2 py-1 rounded ${
                      routing.status === 'released' ? 'bg-green-500/20 text-green-300' :
                      routing.status === 'draft' ? 'bg-yellow-500/20 text-yellow-300' :
                      'bg-slate-800 text-slate-100'
                    }`}>
                      {routing.status}
                    </span>
                    <button
                      onClick={(e) => { e.stopPropagation(); handleDeleteRouting(routing); }}
                      className="text-slate-500 hover:text-red-600 p-1"
                      title={routing.status === 'draft' ? 'Delete' : 'Deactivate'}
                    >
                      <TrashIcon className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                <div className="text-xs text-slate-500 mt-1">
                  Rev {routing.revision} | {routing.operations?.length || 0} operations
                </div>
              </div>
            ))}
            {routings.length === 0 && (
              <p className="text-slate-400 text-center py-4">No routings created yet</p>
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
                  <p className="text-slate-400">{selectedRouting.part?.name}</p>
                  <p className="text-sm text-slate-500">Revision {selectedRouting.revision}</p>
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
                <div className="bg-slate-800/50 rounded-lg p-3">
                  <div className="text-sm text-slate-400">Total Setup</div>
                  <div className="text-lg font-semibold">{formatHours(selectedRouting.total_setup_hours)}</div>
                </div>
                <div className="bg-slate-800/50 rounded-lg p-3">
                  <div className="text-sm text-slate-400">Run Time/Unit</div>
                  <div className="text-lg font-semibold">{formatHours(selectedRouting.total_run_hours_per_unit)}</div>
                </div>
                <div className="bg-slate-800/50 rounded-lg p-3">
                  <div className="text-sm text-slate-400">Labor Cost</div>
                  <div className="text-lg font-semibold">${selectedRouting.total_labor_cost.toFixed(2)}</div>
                </div>
                <div className="bg-slate-800/50 rounded-lg p-3">
                  <div className="text-sm text-slate-400">Operations</div>
                  <div className="text-lg font-semibold">{selectedRouting.operations.length}</div>
                </div>
              </div>

              {/* Operations Table */}
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-slate-700">
                  <thead className="bg-slate-800/50">
                    <tr>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Op #</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Operation</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Work Center</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Setup</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Run/Unit</th>
                      <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">Inspect</th>
                      {selectedRouting.status === 'draft' && (
                        <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">Actions</th>
                      )}
                    </tr>
                  </thead>
                  <tbody className="bg-[#151b28] divide-y divide-slate-700">
                    {selectedRouting.operations
                      .sort((a, b) => a.sequence - b.sequence)
                      .map((op) => (
                        <tr key={op.id} className="hover:bg-slate-800/50">
                          <td className="px-4 py-3 font-medium">{op.operation_number}</td>
                          <td className="px-4 py-3">
                            <div>{op.name}</div>
                            {op.description && (
                              <div className="text-xs text-slate-500">{op.description}</div>
                            )}
                          </td>
                          <td className="px-4 py-3">
                            <div className="font-medium">{op.work_center?.code}</div>
                            <div className="text-xs text-slate-500">{op.work_center?.name}</div>
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
                                className="text-slate-500 hover:text-werco-primary mr-2"
                              >
                                <PencilIcon className="h-5 w-5" />
                              </button>
                              <button
                                onClick={() => handleDeleteOperation(op.id)}
                                className="text-slate-500 hover:text-red-500"
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
                <p className="text-slate-400 text-center py-8">No operations defined yet</p>
              )}
            </>
          ) : (
            <div className="text-center py-12 text-slate-400">
              <ArrowPathIcon className="h-12 w-12 mx-auto mb-4 text-slate-400" />
              <p>Select a routing to view operations</p>
            </div>
          )}
        </div>
      </div>

      {/* Create Routing Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-[#151b28] rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4">Create New Routing</h3>
            <form onSubmit={handleCreateRouting} className="space-y-4">
              <div>
                <label className="label">Part</label>
                <div className="relative">
                  <input
                    type="text"
                    value={routingPartSearch}
                    onChange={(e) => {
                      setRoutingPartSearch(e.target.value);
                      setRoutingPartOpen(true);
                      if (newRouting.part_id) {
                        setNewRouting({ ...newRouting, part_id: 0 });
                        setForcedRoutingPart(null);
                      }
                    }}
                    onFocus={() => setRoutingPartOpen(true)}
                    onBlur={() => {
                      window.setTimeout(() => setRoutingPartOpen(false), 150);
                    }}
                    className="input pr-10"
                    placeholder="Search by part number or name..."
                  />
                  {newRouting.part_id ? (
                    <button
                      type="button"
                      onClick={() => {
                        setNewRouting({ ...newRouting, part_id: 0 });
                        setRoutingPartSearch('');
                        setRoutingPartOpen(true);
                        setForcedRoutingPart(null);
                      }}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-400"
                      title="Clear selection"
                    >
                      x
                    </button>
                  ) : (
                    <span className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500">v</span>
                  )}
                  {routingPartOpen && (
                    <div className="absolute z-10 mt-1 w-full rounded-md border border-slate-700 bg-[#151b28] shadow-lg max-h-64 overflow-y-auto">
                      {filteredRoutingParts.length === 0 ? (
                        <div className="px-3 py-2 text-sm text-slate-400">No matching parts found.</div>
                      ) : (
                        filteredRoutingParts.map((part) => (
                          <button
                            type="button"
                            key={part.id}
                            onMouseDown={() => handleSelectRoutingPart(part)}
                            className={`w-full px-3 py-2 text-left hover:bg-slate-800/50 ${
                              part.id === newRouting.part_id ? 'bg-blue-500/10' : ''
                            }`}
                          >
                            <div className="flex items-center justify-between gap-3">
                              <div>
                                <div className="text-sm font-medium text-white">{part.part_number}</div>
                                <div className="text-xs text-slate-400 truncate">{part.name}</div>
                              </div>
                              <span className="text-[10px] uppercase tracking-wide px-2 py-0.5 rounded-full bg-slate-800 text-slate-400">
                                {part.part_type || 'part'}
                              </span>
                            </div>
                          </button>
                        ))
                      )}
                    </div>
                  )}
                </div>
                <div className="text-xs text-slate-400 mt-1">
                  Type to search. Select a result to continue.
                </div>
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
                <button
                  type="button"
                  onClick={() => {
                    setShowCreateModal(false);
                    setForcedRoutingPart(null);
                    setNewRouting({ part_id: 0, revision: 'A', description: '' });
                    const nextParams = new URLSearchParams(searchParams);
                    nextParams.delete('part_id');
                    setSearchParams(nextParams);
                  }}
                  className="btn-secondary"
                >
                  Cancel
                </button>
                <button type="submit" className="btn-primary" disabled={!newRouting.part_id}>Create</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Generate from Drawing Modal */}
      {showGenerateModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" onClick={() => !generating && setShowGenerateModal(false)}>
          <div className="bg-[#151b28] rounded-lg p-6 max-w-4xl w-full mx-4 max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-3 mb-4">
              <SparklesIcon className="h-6 w-6 text-werco-primary" />
              <h3 className="text-lg font-semibold">Generate Routing from Drawing</h3>
            </div>

            {!generationResult ? (
              <div className="space-y-4">
                <p className="text-sm text-slate-400">
                  Upload a drawing (PDF, DXF, or STEP) and select a part. The system will analyze the drawing and propose a draft routing with operations mapped to your work centers.
                </p>

                {/* Part selector */}
                <div>
                  <label className="label">Part</label>
                  <div className="relative">
                    <input
                      type="text"
                      value={generatePartSearch}
                      onChange={(e) => {
                        setGeneratePartSearch(e.target.value);
                        setGeneratePartOpen(true);
                        if (generatePartId) setGeneratePartId(0);
                      }}
                      onFocus={() => setGeneratePartOpen(true)}
                      onBlur={() => window.setTimeout(() => setGeneratePartOpen(false), 150)}
                      className="input"
                      placeholder="Search by part number or name..."
                    />
                    {generatePartOpen && (
                      <div className="absolute z-10 mt-1 w-full rounded-md border border-slate-700 bg-[#151b28] shadow-lg max-h-48 overflow-y-auto">
                        {filteredGenerateParts.length === 0 ? (
                          <div className="px-3 py-2 text-sm text-slate-400">No matching parts found.</div>
                        ) : (
                          filteredGenerateParts.map((part) => (
                            <button
                              type="button"
                              key={part.id}
                              onMouseDown={() => {
                                setGeneratePartId(part.id);
                                setGeneratePartSearch(`${part.part_number} - ${part.name}`);
                                setGeneratePartOpen(false);
                              }}
                              className={`w-full px-3 py-2 text-left hover:bg-slate-800/50 ${part.id === generatePartId ? 'bg-blue-500/10' : ''}`}
                            >
                              <div className="text-sm font-medium">{part.part_number}</div>
                              <div className="text-xs text-slate-400">{part.name}</div>
                            </button>
                          ))
                        )}
                      </div>
                    )}
                  </div>
                </div>

                {/* File upload */}
                <div>
                  <label className="label">Drawing File</label>
                  <div
                    className="border-2 border-dashed border-slate-600 rounded-lg p-6 text-center cursor-pointer hover:border-werco-primary transition-colors"
                    onClick={() => fileInputRef.current?.click()}
                  >
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept=".pdf,.dxf,.step,.stp"
                      className="hidden"
                      onChange={(e) => setGenerateFile(e.target.files?.[0] || null)}
                    />
                    <DocumentArrowUpIcon className="h-10 w-10 mx-auto text-slate-500 mb-2" />
                    {generateFile ? (
                      <p className="text-sm font-medium text-werco-primary">{generateFile.name}</p>
                    ) : (
                      <>
                        <p className="text-sm text-slate-400">Click to select a file</p>
                        <p className="text-xs text-slate-500 mt-1">Supports PDF, DXF, STEP (.stp)</p>
                      </>
                    )}
                  </div>
                </div>

                <div className="flex justify-end gap-3 pt-2">
                  <button type="button" onClick={() => setShowGenerateModal(false)} className="btn-secondary">
                    Cancel
                  </button>
                  <button
                    onClick={handleAnalyzeDrawing}
                    disabled={!generatePartId || !generateFile || generating}
                    className="btn-primary flex items-center"
                  >
                    {generating ? (
                      <>
                        <div className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent mr-2" />
                        Analyzing...
                      </>
                    ) : (
                      <>
                        <SparklesIcon className="h-4 w-4 mr-2" />
                        Analyze Drawing
                      </>
                    )}
                  </button>
                </div>
              </div>
            ) : (
              <div className="space-y-4">
                {/* Drawing info summary */}
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                  <div className="bg-slate-800/50 rounded-lg p-3">
                    <div className="text-xs text-slate-400">Part</div>
                    <div className="font-semibold text-sm">{generationResult.part_number}</div>
                    <div className="text-xs text-slate-500">{generationResult.part_name}</div>
                  </div>
                  <div className="bg-slate-800/50 rounded-lg p-3">
                    <div className="text-xs text-slate-400">Material</div>
                    <div className="font-semibold text-sm">{generationResult.drawing_info.material || 'Not detected'}</div>
                    {generationResult.drawing_info.thickness && (
                      <div className="text-xs text-slate-500">{generationResult.drawing_info.thickness}</div>
                    )}
                  </div>
                  <div className="bg-slate-800/50 rounded-lg p-3">
                    <div className="text-xs text-slate-400">Finish</div>
                    <div className="font-semibold text-sm">{generationResult.drawing_info.finish || 'None specified'}</div>
                  </div>
                  <div className="bg-slate-800/50 rounded-lg p-3">
                    <div className="text-xs text-slate-400">Confidence</div>
                    <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${confidenceBadge[generationResult.extraction_confidence] || 'bg-slate-800 text-slate-400'}`}>
                      {generationResult.extraction_confidence}
                    </span>
                  </div>
                </div>

                {/* Geometry info row */}
                {(generationResult.drawing_info.cut_length || generationResult.drawing_info.hole_count || generationResult.drawing_info.bend_count) && (
                  <div className="flex gap-4 text-sm text-slate-400">
                    {generationResult.drawing_info.cut_length && (
                      <span>Cut: {generationResult.drawing_info.cut_length.toFixed(1)}"</span>
                    )}
                    {generationResult.drawing_info.hole_count != null && generationResult.drawing_info.hole_count > 0 && (
                      <span>Holes: {generationResult.drawing_info.hole_count}</span>
                    )}
                    {generationResult.drawing_info.bend_count != null && generationResult.drawing_info.bend_count > 0 && (
                      <span>Bends: {generationResult.drawing_info.bend_count}</span>
                    )}
                    {generationResult.drawing_info.flat_length && generationResult.drawing_info.flat_width && (
                      <span>Size: {generationResult.drawing_info.flat_length.toFixed(1)}" x {generationResult.drawing_info.flat_width.toFixed(1)}"</span>
                    )}
                  </div>
                )}

                {/* Warnings */}
                {(generationResult.warnings.length > 0 || generationResult.existing_routing_warning) && (
                  <div className="space-y-2">
                    {generationResult.existing_routing_warning && (
                      <div className="flex items-start gap-2 bg-amber-500/10 text-amber-300 text-sm px-3 py-2 rounded-lg">
                        <ExclamationTriangleIcon className="h-5 w-5 flex-shrink-0 mt-0.5" />
                        {generationResult.existing_routing_warning}
                      </div>
                    )}
                    {generationResult.warnings.map((w, i) => (
                      <div key={i} className="flex items-start gap-2 bg-yellow-500/10 text-yellow-300 text-sm px-3 py-2 rounded-lg">
                        <ExclamationTriangleIcon className="h-4 w-4 flex-shrink-0 mt-0.5" />
                        {w}
                      </div>
                    ))}
                  </div>
                )}

                {/* Editable operations table */}
                <div>
                  <h4 className="font-semibold text-sm mb-2">Proposed Operations ({editedOperations.length})</h4>
                  <div className="overflow-x-auto">
                    <table className="min-w-full divide-y divide-slate-700 text-sm">
                      <thead className="bg-slate-800/50">
                        <tr>
                          <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Seq</th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Operation</th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Work Center</th>
                          <th className="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase">Setup</th>
                          <th className="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase">Run/Unit</th>
                          <th className="px-3 py-2 text-center text-xs font-medium text-slate-400 uppercase">Conf.</th>
                          <th className="px-3 py-2 text-center text-xs font-medium text-slate-400 uppercase w-10"></th>
                        </tr>
                      </thead>
                      <tbody className="bg-[#151b28] divide-y divide-slate-700">
                        {editedOperations.map((op, idx) => (
                          <tr key={idx} className="hover:bg-slate-800/50">
                            <td className="px-3 py-2 font-medium">{op.sequence}</td>
                            <td className="px-3 py-2">
                              <input
                                type="text"
                                value={op.operation_name}
                                onChange={(e) => updateEditedOp(idx, 'operation_name', e.target.value)}
                                className="input py-1 text-sm w-full"
                              />
                              {op.description && (
                                <div className="text-xs text-slate-500 mt-0.5">{op.description}</div>
                              )}
                            </td>
                            <td className="px-3 py-2">
                              <select
                                value={op.work_center_id || 0}
                                onChange={(e) => {
                                  const wcId = parseInt(e.target.value);
                                  const wc = workCenters.find((w) => w.id === wcId);
                                  updateEditedOp(idx, 'work_center_id', wcId || undefined);
                                  updateEditedOp(idx, 'work_center_name', wc?.name || undefined);
                                }}
                                className={`input py-1 text-sm w-full ${!op.work_center_id ? 'border-red-300' : ''}`}
                              >
                                <option value={0}>Select...</option>
                                {workCenters.map((wc) => (
                                  <option key={wc.id} value={wc.id}>
                                    {wc.code} - {wc.name}
                                  </option>
                                ))}
                              </select>
                              {!op.work_center_id && (
                                <div className="text-xs text-red-500 mt-0.5">Required</div>
                              )}
                            </td>
                            <td className="px-3 py-2 text-right">
                              <input
                                type="number"
                                value={Math.round(op.setup_hours * 60 * 100) / 100}
                                onChange={(e) => updateEditedOp(idx, 'setup_hours', (parseFloat(e.target.value) || 0) / 60)}
                                className="input py-1 text-sm w-20 text-right"
                                step={1}
                                min={0}
                              />
                              <span className="text-xs text-slate-500 ml-1">min</span>
                            </td>
                            <td className="px-3 py-2 text-right">
                              <input
                                type="number"
                                value={Math.round(op.run_hours_per_unit * 60 * 100) / 100}
                                onChange={(e) => updateEditedOp(idx, 'run_hours_per_unit', (parseFloat(e.target.value) || 0) / 60)}
                                className="input py-1 text-sm w-20 text-right"
                                step={0.1}
                                min={0}
                              />
                              <span className="text-xs text-slate-500 ml-1">min</span>
                            </td>
                            <td className="px-3 py-2 text-center">
                              <span className={`inline-flex px-1.5 py-0.5 rounded text-xs font-medium ${confidenceBadge[op.confidence] || 'bg-slate-800 text-slate-400'}`}>
                                {op.confidence}
                              </span>
                            </td>
                            <td className="px-3 py-2 text-center">
                              <button
                                onClick={() => removeEditedOp(idx)}
                                className="text-slate-500 hover:text-red-500"
                                title="Remove operation"
                              >
                                <TrashIcon className="h-4 w-4" />
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {editedOperations.length === 0 && (
                    <p className="text-sm text-slate-400 text-center py-4">No operations proposed. The drawing may not have enough information.</p>
                  )}
                </div>

                <div className="flex justify-between items-center pt-2">
                  <button
                    type="button"
                    onClick={() => {
                      setGenerationResult(null);
                      setEditedOperations([]);
                      setGenerateFile(null);
                    }}
                    className="text-sm text-slate-400 hover:text-slate-300"
                  >
                    Start over
                  </button>
                  <div className="flex gap-3">
                    <button type="button" onClick={() => setShowGenerateModal(false)} className="btn-secondary">
                      Cancel
                    </button>
                    <button
                      onClick={handleCreateFromGeneration}
                      disabled={creatingFromGeneration || editedOperations.length === 0 || editedOperations.some((op) => !op.work_center_id)}
                      className="btn-primary flex items-center"
                    >
                      {creatingFromGeneration ? (
                        <>
                          <div className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent mr-2" />
                          Creating...
                        </>
                      ) : (
                        <>
                          <PlusIcon className="h-4 w-4 mr-2" />
                          Create Draft Routing
                        </>
                      )}
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Add/Edit Operation Modal */}
      {showAddOperationModal && selectedRouting && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50" onClick={() => setShowAddOperationModal(false)}>
          <div className="bg-[#151b28] rounded-lg p-6 max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
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
                      className="border border-slate-600 rounded-lg px-3 py-2 w-20 bg-[#151b28] cursor-pointer focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
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
                      className="border border-slate-600 rounded-lg px-3 py-2 w-20 bg-[#151b28] cursor-pointer focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
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
                      className="border border-slate-600 rounded-lg px-3 py-2 w-20 bg-[#151b28] cursor-pointer focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
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
                      className="border border-slate-600 rounded-lg px-3 py-2 w-20 bg-[#151b28] cursor-pointer focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
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
