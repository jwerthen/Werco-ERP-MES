import React, { useEffect, useMemo, useState, useRef } from 'react';
import api from '../services/api';
import { Modal } from '../components/ui/Modal';
import { EmptyState, ErrorState, useToast } from '../components/ui';
import { FormField } from '../components/ui/FormField';
import { RoutingImportWizard } from '../components/routing/RoutingImportWizard';
import { useAuth } from '../context/AuthContext';
import { hasPermission } from '../utils/permissions';
import { useSearchParams } from 'react-router-dom';
import {
  PlusIcon,
  PencilIcon,
  TrashIcon,
  CheckCircleIcon,
  ArrowPathIcon,
  ArrowUpTrayIcon,
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
  cycle_time_seconds?: number | null;
  pieces_per_cycle?: number | null;
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
  generation_session_id?: number;
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
  const { user } = useAuth();
  const { showToast } = useToast();
  // Importing routings creates draft records — gate it to the same roles the
  // backend allows (ADMIN / MANAGER / SUPERVISOR all hold routings:create).
  const canImport = hasPermission(user?.role, 'routings:create');
  // Editing a RELEASED routing's time standards is release-adjacent authority:
  // the backend gates it to Admin/Manager (Supervisor 403'd) plus the
  // platform_admin / superuser escalation. routings:release is held by exactly
  // admin / manager / platform_admin, so mirror the server gate with it.
  const canEditReleasedTimes =
    hasPermission(user?.role, 'routings:release') || user?.is_superuser === true;

  const [routings, setRoutings] = useState<Routing[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [selectedRouting, setSelectedRouting] = useState<Routing | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
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
    cycle_time_seconds: 0,
    pieces_per_cycle: 1,
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
    setLoading(true);
    setLoadError(false);
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
      setLoadError(true);
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
      showToast('error', 'Failed to load routing details.');
    }
    return null;
  };

  useEffect(() => {
    const routingIdParam = searchParams.get('id');
    if (!routingIdParam) return;
    const routingId = parseInt(routingIdParam, 10);
    if (Number.isNaN(routingId) || selectedRouting?.id === routingId) return;
    loadRouting(routingId);
  }, [searchParams, selectedRouting?.id]);

  const handleCreateRouting = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newRouting.part_id) {
      showToast('error', 'Select a part before creating a routing.');
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
      showToast('error', err.response?.data?.detail || 'Failed to create routing');
    }
  };

  const handleAddOperation = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedRouting) return;

    // A RELEASED routing only accepts time-standard edits. Send just those fields
    // so an unchanged structural field can never trip the backend's 400 guard.
    const releasedEdit = isReleasedEdit;

    try {
      if (editingOperation) {
        const payload = releasedEdit
          ? {
              setup_hours: newOperation.setup_hours,
              run_hours_per_unit: newOperation.run_hours_per_unit,
              move_hours: newOperation.move_hours,
              queue_hours: newOperation.queue_hours,
              cycle_time_seconds: newOperation.cycle_time_seconds || null,
              pieces_per_cycle: newOperation.pieces_per_cycle,
            }
          : newOperation;
        await api.updateRoutingOperation(selectedRouting.id, editingOperation.id, payload);
      } else {
        await api.addRoutingOperation(selectedRouting.id, newOperation);
      }
      await loadRouting(selectedRouting.id);
      setShowAddOperationModal(false);
      setEditingOperation(null);
      resetOperationForm();
    } catch (err: any) {
      const status = err.response?.status;
      if (status === 403) {
        showToast('error', "You need the Admin or Manager role to edit a released routing's time standards.");
      } else {
        // 400 surfaces the server's "only time standards…" message; other codes fall back.
        showToast('error', err.response?.data?.detail || 'Failed to save operation');
      }
    }
  };

  const handleDeleteOperation = async (operationId: number) => {
    if (!selectedRouting || !window.confirm('Delete this operation?')) return;

    try {
      await api.deleteRoutingOperation(selectedRouting.id, operationId);
      await loadRouting(selectedRouting.id);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to delete operation');
    }
  };

  const handleReleaseRouting = async () => {
    if (!selectedRouting) return;

    try {
      await api.releaseRouting(selectedRouting.id);
      await loadRouting(selectedRouting.id);
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to release routing');
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
      showToast('error', err.response?.data?.detail || 'Failed to delete routing');
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
      cycle_time_seconds: op.cycle_time_seconds ?? 0,
      pieces_per_cycle: op.pieces_per_cycle ?? 1,
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
      cycle_time_seconds: 0,
      pieces_per_cycle: 1,
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
      showToast('error', err.response?.data?.detail || 'Failed to analyze drawing');
    } finally {
      setGenerating(false);
    }
  };

  const handleCreateFromGeneration = async () => {
    if (!generationResult || editedOperations.length === 0) return;
    setCreatingFromGeneration(true);
    try {
      const invalidOperation = editedOperations.find((op) => !op.work_center_id || !op.operation_name.trim());
      if (invalidOperation) {
        showToast('error', 'Every proposed operation needs a name and work center before creating the routing.');
        return;
      }
      const operations = editedOperations
        .map((op) => ({
          sequence: op.sequence,
          name: op.operation_name.trim(),
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
        showToast('error', 'All operations need a work center assigned before creating the routing.');
        return;
      }
      const created = await api.createRoutingFromGeneration({
        part_id: generationResult.part_id,
        generation_session_id: generationResult.generation_session_id,
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
      showToast('error', err.response?.data?.detail || 'Failed to create routing');
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

  const updateEditedOpFields = (index: number, updates: Partial<ProposedOperation>) => {
    setEditedOperations((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], ...updates };
      return next;
    });
  };

  const removeEditedOp = (index: number) => {
    setEditedOperations((prev) => prev.filter((_, i) => i !== index));
  };

  const addEditedOperation = () => {
    const nextSeq = editedOperations.length
      ? Math.max(...editedOperations.map((op) => op.sequence || 0)) + 10
      : 10;
    const defaultWorkCenter = workCenters[0];
    setEditedOperations((prev) => ([
      ...prev,
      {
        sequence: nextSeq,
        operation_name: 'New Operation',
        description: '',
        work_center_type: defaultWorkCenter?.work_center_type || 'fabrication',
        work_center_id: defaultWorkCenter?.id,
        work_center_name: defaultWorkCenter?.name,
        setup_hours: 0,
        run_hours_per_unit: 0,
        is_inspection_point: false,
        is_outside_operation: false,
        tooling_requirements: '',
        work_instructions: '',
        confidence: 'manual',
      },
    ]));
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

  // True when the open Add/Edit modal is editing an operation on a RELEASED
  // routing — drives the time-standards-only modal variant and the save payload.
  const isReleasedEdit = !!editingOperation && selectedRouting?.status === 'released';

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
        message="Could not load routings, parts, or work centers."
        onRetry={loadData}
        className="h-64"
      />
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
          {canImport && (
            <button
              onClick={() => setShowImportModal(true)}
              className="btn-secondary flex items-center"
            >
              <ArrowUpTrayIcon className="h-5 w-5 mr-2" />
              Import Routings
            </button>
          )}
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
                role="button"
                tabIndex={0}
                onClick={() => loadRouting(routing.id)}
                onKeyDown={(e) => {
                  if (e.target !== e.currentTarget) return;
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    loadRouting(routing.id);
                  }
                }}
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
              <EmptyState
                icon={ArrowPathIcon}
                title="No routings yet"
                description="Create a routing to define the operations a part moves through."
                action={{
                  label: 'New Routing',
                  onClick: () => {
                    setNewRouting({ part_id: 0, revision: 'A', description: '' });
                    setForcedRoutingPart(null);
                    setShowCreateModal(true);
                  },
                }}
              />
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

              {/* Released routings: hairline policy note. Structural edits are
                  blocked; only time standards can be adjusted as actuals come in. */}
              {selectedRouting.status === 'released' && (
                <div className="mb-4 flex items-start gap-2 border border-werco-primary/40 bg-werco-primary/5 px-3 py-2 text-xs text-slate-300">
                  <CheckCircleIcon className="h-4 w-4 flex-shrink-0 mt-0.5 text-werco-primary" />
                  <span>
                    Released routing — process is locked.
                    {canEditReleasedTimes
                      ? ' Time standards (setup, run, move, queue, cycle) can be adjusted from each operation row; to change the process, create a new revision.'
                      : ' Editing time standards requires the Admin or Manager role.'}
                  </span>
                </div>
              )}

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
                      {(selectedRouting.status === 'draft' ||
                        (selectedRouting.status === 'released' && canEditReleasedTimes)) && (
                        <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">Actions</th>
                      )}
                    </tr>
                  </thead>
                  <tbody className="bg-fd-panel divide-y divide-slate-700">
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
                          {selectedRouting.status === 'draft' ? (
                            <td className="px-4 py-3 text-center">
                              <button
                                onClick={() => openEditOperation(op)}
                                className="text-slate-500 hover:text-werco-primary mr-2"
                                title="Edit operation"
                              >
                                <PencilIcon className="h-5 w-5" />
                              </button>
                              <button
                                onClick={() => handleDeleteOperation(op.id)}
                                className="text-slate-500 hover:text-red-500"
                                title="Delete operation"
                              >
                                <TrashIcon className="h-5 w-5" />
                              </button>
                            </td>
                          ) : (
                            selectedRouting.status === 'released' && canEditReleasedTimes && (
                              // Released: time standards only — no delete (structural change blocked).
                              <td className="px-4 py-3 text-center">
                                <button
                                  onClick={() => openEditOperation(op)}
                                  className="inline-flex items-center gap-1 text-slate-500 hover:text-werco-primary"
                                  title="Edit time standards"
                                >
                                  <PencilIcon className="h-5 w-5" />
                                  <span className="text-xs">Edit times</span>
                                </button>
                              </td>
                            )
                          )}
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>

              {selectedRouting.operations.length === 0 && (
                <EmptyState
                  icon={PlusIcon}
                  title="No operations defined yet"
                  description="Add the operations this part moves through to build out the routing."
                  action={
                    selectedRouting.status === 'draft'
                      ? { label: 'Add Operation', onClick: openAddOperationModal }
                      : undefined
                  }
                />
              )}
            </>
          ) : (
            <EmptyState
              icon={ArrowPathIcon}
              title="No routing selected"
              description="Select a routing from the list to view its operations."
            />
          )}
        </div>
      </div>

      {/* Create Routing Modal */}
      <Modal
        open={showCreateModal}
        onClose={() => {
          setShowCreateModal(false);
          setForcedRoutingPart(null);
          setNewRouting({ part_id: 0, revision: 'A', description: '' });
          const nextParams = new URLSearchParams(searchParams);
          nextParams.delete('part_id');
          setSearchParams(nextParams);
        }}
        size="md"
        closeOnBackdrop={false}
      >
            <h3 className="text-lg font-semibold mb-4">Create New Routing</h3>
            <form onSubmit={handleCreateRouting} className="space-y-4">
              <div>
                <label htmlFor="routing-part-search" className="label">Part</label>
                <div className="relative">
                  <input
                    id="routing-part-search"
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
                    aria-label="Search parts by number or name"
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
                    <div className="absolute z-10 mt-1 w-full rounded-md border border-slate-700 bg-fd-panel shadow-lg max-h-64 overflow-y-auto">
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
                            aria-label={`Select part ${part.part_number}`}
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
              <FormField label="Revision" required>
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={newRouting.revision}
                    onChange={(e) => setNewRouting({ ...newRouting, revision: e.target.value })}
                    className="input"
                    required
                  />
                )}
              </FormField>
              <FormField label="Description">
                {(field) => (
                  <textarea
                    {...field}
                    value={newRouting.description}
                    onChange={(e) => setNewRouting({ ...newRouting, description: e.target.value })}
                    className="input"
                    rows={2}
                  />
                )}
              </FormField>
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
      </Modal>

      {/* Generate from Drawing Modal */}
      <Modal
        open={showGenerateModal}
        onClose={() => setShowGenerateModal(false)}
        size="4xl"
        closeOnBackdrop={!generating}
        closeOnEscape={!generating}
      >
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
                  <label htmlFor="generate-part-search" className="label">Part</label>
                  <div className="relative">
                    <input
                      id="generate-part-search"
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
                      aria-label="Search parts by number or name"
                    />
                    {generatePartOpen && (
                      <div className="absolute z-10 mt-1 w-full rounded-md border border-slate-700 bg-fd-panel shadow-lg max-h-48 overflow-y-auto">
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
                  <label htmlFor="generate-drawing-file" className="label">Drawing File</label>
                  <div
                    role="button"
                    tabIndex={0}
                    className="border-2 border-dashed border-slate-600 rounded-lg p-6 text-center cursor-pointer hover:border-werco-primary transition-colors"
                    onClick={() => fileInputRef.current?.click()}
                    onKeyDown={(e) => {
                      if (e.target !== e.currentTarget) return;
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        fileInputRef.current?.click();
                      }
                    }}
                  >
                    <input
                      id="generate-drawing-file"
                      ref={fileInputRef}
                      type="file"
                      accept=".pdf,.dxf,.step,.stp"
                      className="hidden"
                      onChange={(e) => setGenerateFile(e.target.files?.[0] || null)}
                      aria-label="Upload drawing file"
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
                  <div className="flex items-center justify-between mb-2">
                    <h4 className="font-semibold text-sm">Proposed Operations ({editedOperations.length})</h4>
                    <button type="button" onClick={addEditedOperation} className="btn-secondary btn-sm flex items-center">
                      <PlusIcon className="h-4 w-4 mr-1" />
                      Add Operation
                    </button>
                  </div>
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
                          <th className="px-3 py-2 text-center text-xs font-medium text-slate-400 uppercase w-10" aria-label="Actions"></th>
                        </tr>
                      </thead>
                      <tbody className="bg-fd-panel divide-y divide-slate-700">
                        {editedOperations.map((op, idx) => (
                          <React.Fragment key={idx}>
                            <tr className="hover:bg-slate-800/50">
                              <td className="px-3 py-2 font-medium">
                                <input
                                  type="number"
                                  value={op.sequence}
                                  onChange={(e) => updateEditedOp(idx, 'sequence', parseInt(e.target.value) || 0)}
                                  className="input py-1 text-sm w-20 text-center"
                                  step={10}
                                  min={10}
                                  aria-label="Operation sequence"
                                />
                              </td>
                              <td className="px-3 py-2">
                                <input
                                  type="text"
                                  value={op.operation_name}
                                  onChange={(e) => updateEditedOp(idx, 'operation_name', e.target.value)}
                                  className={`input py-1 text-sm w-full ${!op.operation_name.trim() ? 'border-red-300' : ''}`}
                                  aria-label="Operation name"
                                />
                              </td>
                              <td className="px-3 py-2">
                                <select
                                  value={op.work_center_id || 0}
                                  onChange={(e) => {
                                    const wcId = parseInt(e.target.value);
                                    const wc = workCenters.find((w) => w.id === wcId);
                                    updateEditedOpFields(idx, {
                                      work_center_id: wcId || undefined,
                                      work_center_name: wc?.name || undefined,
                                      work_center_type: wc?.work_center_type || op.work_center_type,
                                    });
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
                                  aria-label="Setup time in minutes"
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
                                  aria-label="Run time per unit in minutes"
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
                            <tr className="bg-slate-900/30">
                              <td className="px-3 pb-3" aria-hidden="true"></td>
                              <td className="px-3 pb-3" colSpan={6}>
                                <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                                  <FormField label="Description" labelClassName="text-xs">
                                    {(field) => (
                                      <textarea
                                        {...field}
                                        value={op.description || ''}
                                        onChange={(e) => updateEditedOp(idx, 'description', e.target.value)}
                                        className="input py-2 text-sm w-full"
                                        rows={2}
                                      />
                                    )}
                                  </FormField>
                                  <FormField label="Work Instructions" labelClassName="text-xs">
                                    {(field) => (
                                      <textarea
                                        {...field}
                                        value={op.work_instructions || ''}
                                        onChange={(e) => updateEditedOp(idx, 'work_instructions', e.target.value)}
                                        className="input py-2 text-sm w-full"
                                        rows={2}
                                      />
                                    )}
                                  </FormField>
                                </div>
                                <div className="mt-2 flex flex-wrap gap-4 text-xs text-slate-300">
                                  <label className="inline-flex items-center gap-2">
                                    <input
                                      type="checkbox"
                                      checked={op.is_inspection_point}
                                      onChange={(e) => updateEditedOp(idx, 'is_inspection_point', e.target.checked)}
                                      className="rounded border-slate-600 bg-slate-800"
                                      aria-label="Inspection point"
                                    />
                                    Inspection point
                                  </label>
                                  <label className="inline-flex items-center gap-2">
                                    <input
                                      type="checkbox"
                                      checked={op.is_outside_operation}
                                      onChange={(e) => updateEditedOp(idx, 'is_outside_operation', e.target.checked)}
                                      className="rounded border-slate-600 bg-slate-800"
                                      aria-label="Outside operation"
                                    />
                                    Outside operation
                                  </label>
                                  {op.work_center_type && (
                                    <span className="text-slate-500">Type: {op.work_center_type.replace(/_/g, ' ')}</span>
                                  )}
                                </div>
                              </td>
                            </tr>
                          </React.Fragment>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {editedOperations.length === 0 && (
                    <EmptyState
                      icon={DocumentArrowUpIcon}
                      title="No operations proposed"
                      description="The drawing may not have enough information. Add operations manually to build the routing."
                      action={{ label: 'Add Operation', onClick: addEditedOperation }}
                    />
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
                      disabled={creatingFromGeneration || editedOperations.length === 0 || editedOperations.some((op) => !op.work_center_id || !op.operation_name.trim())}
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
      </Modal>

      {/* Add/Edit Operation Modal */}
      <Modal
        open={showAddOperationModal && !!selectedRouting}
        onClose={() => setShowAddOperationModal(false)}
        size="lg"
      >
            <h3 className="text-lg font-semibold mb-4">
              {isReleasedEdit ? 'Edit Time Standards' : editingOperation ? 'Edit Operation' : 'Add Operation'}
            </h3>
            <form onSubmit={handleAddOperation} className="space-y-4">
              {isReleasedEdit && (
                <div className="flex items-start gap-2 border border-werco-primary/40 bg-werco-primary/5 px-3 py-2 text-xs text-slate-300">
                  <ExclamationTriangleIcon className="h-4 w-4 flex-shrink-0 mt-0.5 text-werco-primary" />
                  <span>
                    Released routing — only time standards are editable. To change the process
                    (sequence, work center, instructions), create a new revision.
                  </span>
                </div>
              )}
              {isReleasedEdit && editingOperation && (
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div>
                    <div className="text-xs uppercase tracking-wide text-slate-500">Operation</div>
                    <div className="font-medium">
                      {editingOperation.operation_number} — {editingOperation.name}
                    </div>
                  </div>
                  <div>
                    <div className="text-xs uppercase tracking-wide text-slate-500">Work Center</div>
                    <div className="font-medium">
                      {editingOperation.work_center?.code}
                      {editingOperation.work_center?.name ? ` — ${editingOperation.work_center.name}` : ''}
                    </div>
                  </div>
                </div>
              )}
              {!isReleasedEdit && (
                <>
                  <div className="grid grid-cols-2 gap-4">
                    <FormField label="Sequence #" required>
                      {(field) => (
                        <input
                          {...field}
                          type="number"
                          value={newOperation.sequence}
                          onChange={(e) => setNewOperation({ ...newOperation, sequence: parseInt(e.target.value) })}
                          className="input"
                          step={10}
                          required
                        />
                      )}
                    </FormField>
                    <FormField label="Work Center" required>
                      {(field) => (
                        <select
                          {...field}
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
                      )}
                    </FormField>
                  </div>
                  <FormField label="Operation Name" required>
                    {(field) => (
                      <input
                        {...field}
                        type="text"
                        value={newOperation.name}
                        onChange={(e) => setNewOperation({ ...newOperation, name: e.target.value })}
                        className="input"
                        placeholder="e.g., Cut to size, Weld assembly, Paint"
                        required
                      />
                    )}
                  </FormField>
                  <FormField label="Description">
                    {(field) => (
                      <textarea
                        {...field}
                        value={newOperation.description}
                        onChange={(e) => setNewOperation({ ...newOperation, description: e.target.value })}
                        className="input"
                        rows={2}
                      />
                    )}
                  </FormField>
                </>
              )}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label htmlFor="operation-setup-time" className="label">Setup Time</label>
                  <div className="flex gap-2">
                    <input
                      id="operation-setup-time"
                      type="number"
                      value={timeUnits.setup === 'min' ? Math.round(newOperation.setup_hours * 60 * 100) / 100 : newOperation.setup_hours}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setNewOperation({ ...newOperation, setup_hours: timeUnits.setup === 'min' ? Math.round(val / 60 * 10000) / 10000 : val });
                      }}
                      className="input flex-1"
                      step={timeUnits.setup === 'min' ? 1 : 0.01}
                      min={0}
                      aria-label="Setup time"
                    />
                    <select
                      value={timeUnits.setup}
                      onChange={(e) => setTimeUnits({ ...timeUnits, setup: e.target.value as 'hrs' | 'min' })}
                      className="border border-slate-600 rounded-lg px-3 py-2 w-20 bg-fd-panel cursor-pointer focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    >
                      <option value="min">min</option>
                      <option value="hrs">hrs</option>
                    </select>
                  </div>
                </div>
                <div>
                  <label htmlFor="operation-run-time" className="label">Run Time/Unit</label>
                  <div className="flex gap-2">
                    <input
                      id="operation-run-time"
                      type="number"
                      value={timeUnits.run === 'min' ? Math.round(newOperation.run_hours_per_unit * 60 * 100) / 100 : newOperation.run_hours_per_unit}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setNewOperation({ ...newOperation, run_hours_per_unit: timeUnits.run === 'min' ? Math.round(val / 60 * 10000) / 10000 : val });
                      }}
                      className="input flex-1"
                      step={timeUnits.run === 'min' ? 0.1 : 0.001}
                      min={0}
                      aria-label="Run time per unit"
                    />
                    <select
                      value={timeUnits.run}
                      onChange={(e) => setTimeUnits({ ...timeUnits, run: e.target.value as 'hrs' | 'min' })}
                      className="border border-slate-600 rounded-lg px-3 py-2 w-20 bg-fd-panel cursor-pointer focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    >
                      <option value="min">min</option>
                      <option value="hrs">hrs</option>
                    </select>
                  </div>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label htmlFor="operation-move-time" className="label">Move Time</label>
                  <div className="flex gap-2">
                    <input
                      id="operation-move-time"
                      type="number"
                      value={timeUnits.move === 'min' ? Math.round(newOperation.move_hours * 60 * 100) / 100 : newOperation.move_hours}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setNewOperation({ ...newOperation, move_hours: timeUnits.move === 'min' ? Math.round(val / 60 * 10000) / 10000 : val });
                      }}
                      className="input flex-1"
                      step={timeUnits.move === 'min' ? 1 : 0.01}
                      min={0}
                      aria-label="Move time"
                    />
                    <select
                      value={timeUnits.move}
                      onChange={(e) => setTimeUnits({ ...timeUnits, move: e.target.value as 'hrs' | 'min' })}
                      className="border border-slate-600 rounded-lg px-3 py-2 w-20 bg-fd-panel cursor-pointer focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    >
                      <option value="min">min</option>
                      <option value="hrs">hrs</option>
                    </select>
                  </div>
                </div>
                <div>
                  <label htmlFor="operation-queue-time" className="label">Queue Time</label>
                  <div className="flex gap-2">
                    <input
                      id="operation-queue-time"
                      type="number"
                      value={timeUnits.queue === 'min' ? Math.round(newOperation.queue_hours * 60 * 100) / 100 : newOperation.queue_hours}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setNewOperation({ ...newOperation, queue_hours: timeUnits.queue === 'min' ? Math.round(val / 60 * 10000) / 10000 : val });
                      }}
                      className="input flex-1"
                      step={timeUnits.queue === 'min' ? 1 : 0.01}
                      min={0}
                      aria-label="Queue time"
                    />
                    <select
                      value={timeUnits.queue}
                      onChange={(e) => setTimeUnits({ ...timeUnits, queue: e.target.value as 'hrs' | 'min' })}
                      className="border border-slate-600 rounded-lg px-3 py-2 w-20 bg-fd-panel cursor-pointer focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    >
                      <option value="min">min</option>
                      <option value="hrs">hrs</option>
                    </select>
                  </div>
                </div>
              </div>
              {/* Machine cycle fields — time standards, editable on released routings too. */}
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label htmlFor="operation-cycle-time" className="label">Cycle Time</label>
                  <div className="flex gap-2">
                    <input
                      id="operation-cycle-time"
                      type="number"
                      value={newOperation.cycle_time_seconds}
                      onChange={(e) =>
                        setNewOperation({ ...newOperation, cycle_time_seconds: parseFloat(e.target.value) || 0 })
                      }
                      className="input flex-1"
                      step={1}
                      min={0}
                      aria-label="Cycle time in seconds"
                    />
                    <span className="inline-flex items-center px-3 py-2 w-20 text-sm text-slate-400 border border-slate-700 rounded-lg bg-fd-panel">
                      sec
                    </span>
                  </div>
                </div>
                <FormField label="Pieces / Cycle">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      value={newOperation.pieces_per_cycle}
                      onChange={(e) =>
                        setNewOperation({ ...newOperation, pieces_per_cycle: parseInt(e.target.value) || 1 })
                      }
                      className="input"
                      step={1}
                      min={1}
                    />
                  )}
                </FormField>
              </div>
              {!isReleasedEdit && (
                <div className="flex gap-6">
                  <label className="flex items-center">
                    <input
                      type="checkbox"
                      checked={newOperation.is_inspection_point}
                      onChange={(e) => setNewOperation({ ...newOperation, is_inspection_point: e.target.checked })}
                      className="mr-2"
                      aria-label="Inspection Point"
                    />
                    <span className="text-sm">Inspection Point</span>
                  </label>
                  <label className="flex items-center">
                    <input
                      type="checkbox"
                      checked={newOperation.is_outside_operation}
                      onChange={(e) => setNewOperation({ ...newOperation, is_outside_operation: e.target.checked })}
                      className="mr-2"
                      aria-label="Outside Operation"
                    />
                    <span className="text-sm">Outside Operation</span>
                  </label>
                </div>
              )}
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
                  {isReleasedEdit ? 'Save Time Standards' : `${editingOperation ? 'Update' : 'Add'} Operation`}
                </button>
              </div>
            </form>
      </Modal>

      {/* Routing Import Wizard */}
      {canImport && showImportModal && (
        <RoutingImportWizard
          onComplete={async () => {
            await loadData();
          }}
          onClose={() => setShowImportModal(false)}
        />
      )}
    </div>
  );
}
