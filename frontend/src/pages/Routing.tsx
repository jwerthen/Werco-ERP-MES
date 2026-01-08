import React, { useEffect, useState } from 'react';
import api from '../services/api';
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

  useEffect(() => {
    loadData();
  }, []);

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
    } catch (err) {
      console.error('Failed to load routing:', err);
    }
  };

  const handleCreateRouting = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const created = await api.createRouting(newRouting);
      setRoutings([created, ...routings]);
      setSelectedRouting(created);
      setShowCreateModal(false);
      setNewRouting({ part_id: 0, revision: 'A', description: '' });
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
        <h1 className="text-2xl font-bold text-gray-900">Operations Routing</h1>
        <button onClick={() => setShowCreateModal(true)} className="btn-primary flex items-center">
          <PlusIcon className="h-5 w-5 mr-2" />
          New Routing
        </button>
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
                  <div>
                    <div className="font-medium">{routing.part?.part_number}</div>
                    <div className="text-sm text-gray-500">{routing.part?.name}</div>
                  </div>
                  <span className={`text-xs px-2 py-1 rounded ${
                    routing.status === 'released' ? 'bg-green-100 text-green-800' :
                    routing.status === 'draft' ? 'bg-yellow-100 text-yellow-800' :
                    'bg-gray-100 text-gray-800'
                  }`}>
                    {routing.status}
                  </span>
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
                    .filter(p => ['assembly', 'manufactured'].includes(p.part_type))
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
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto">
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
                      value={timeUnits.setup === 'min' ? newOperation.setup_hours * 60 : newOperation.setup_hours}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setNewOperation({ ...newOperation, setup_hours: timeUnits.setup === 'min' ? val / 60 : val });
                      }}
                      className="input flex-1"
                      step={timeUnits.setup === 'min' ? 1 : 0.01}
                      min={0}
                    />
                    <select
                      value={timeUnits.setup}
                      onChange={(e) => setTimeUnits({ ...timeUnits, setup: e.target.value as 'hrs' | 'min' })}
                      className="input w-20"
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
                      value={timeUnits.run === 'min' ? newOperation.run_hours_per_unit * 60 : newOperation.run_hours_per_unit}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setNewOperation({ ...newOperation, run_hours_per_unit: timeUnits.run === 'min' ? val / 60 : val });
                      }}
                      className="input flex-1"
                      step={timeUnits.run === 'min' ? 0.1 : 0.001}
                      min={0}
                    />
                    <select
                      value={timeUnits.run}
                      onChange={(e) => setTimeUnits({ ...timeUnits, run: e.target.value as 'hrs' | 'min' })}
                      className="input w-20"
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
                      value={timeUnits.move === 'min' ? newOperation.move_hours * 60 : newOperation.move_hours}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setNewOperation({ ...newOperation, move_hours: timeUnits.move === 'min' ? val / 60 : val });
                      }}
                      className="input flex-1"
                      step={timeUnits.move === 'min' ? 1 : 0.01}
                      min={0}
                    />
                    <select
                      value={timeUnits.move}
                      onChange={(e) => setTimeUnits({ ...timeUnits, move: e.target.value as 'hrs' | 'min' })}
                      className="input w-20"
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
                      value={timeUnits.queue === 'min' ? newOperation.queue_hours * 60 : newOperation.queue_hours}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        setNewOperation({ ...newOperation, queue_hours: timeUnits.queue === 'min' ? val / 60 : val });
                      }}
                      className="input flex-1"
                      step={timeUnits.queue === 'min' ? 1 : 0.01}
                      min={0}
                    />
                    <select
                      value={timeUnits.queue}
                      onChange={(e) => setTimeUnits({ ...timeUnits, queue: e.target.value as 'hrs' | 'min' })}
                      className="input w-20"
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
