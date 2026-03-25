import React, { useEffect, useState, useMemo } from 'react';
import api from '../../services/api';
import { Part } from '../../types';
import {
  Routing, RoutingOperation, WorkCenter, formatHours,
} from '../../types/engineering';
import { useToast } from '../ui/Toast';
import { StatusBadge } from '../ui/StatusBadge';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import {
  PlusIcon,
  PencilIcon,
  TrashIcon,
  CheckCircleIcon,
  WrenchScrewdriverIcon,
  ArrowUturnLeftIcon,
} from '@heroicons/react/24/outline';

interface Props {
  part: Part;
  routing: Routing | null;
  onRoutingChanged: () => Promise<void>;
}

type TimeUnit = 'hrs' | 'min';

export function PartRoutingTab({ part, routing, onRoutingChanged }: Props) {
  const { showToast } = useToast();

  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [showOpModal, setShowOpModal] = useState(false);
  const [editingOp, setEditingOp] = useState<RoutingOperation | null>(null);
  const [creating, setCreating] = useState(false);
  const [confirmAction, setConfirmAction] = useState<{ title: string; message: string; action: () => void } | null>(null);

  const [opForm, setOpForm] = useState({
    sequence: 10,
    name: '',
    description: '',
    work_center_id: 0,
    setup_hours: 0,
    run_hours_per_unit: 0,
    move_hours: 0,
    queue_hours: 0,
    is_inspection_point: false,
    is_outside_operation: false,
  });

  const [timeUnits, setTimeUnits] = useState<Record<string, TimeUnit>>({
    setup: 'min', run: 'min', move: 'min', queue: 'min',
  });

  useEffect(() => {
    api.getWorkCenters?.()
      .then((data: any) => {
        const wcs = Array.isArray(data) ? data : data?.items || data?.results || [];
        setWorkCenters(wcs);
      })
      .catch(() => {});
  }, []);

  const toDisplay = (hours: number, unit: TimeUnit) =>
    unit === 'min' ? Math.round(hours * 60 * 100) / 100 : hours;

  const toHours = (value: number, unit: TimeUnit) =>
    unit === 'min' ? Math.round((value / 60) * 10000) / 10000 : value;

  // ── Actions ────────────────────────────────────────────────────────────

  const handleCreateRouting = async () => {
    setCreating(true);
    try {
      await api.createRouting({
        part_id: part.id,
        revision: part.revision,
        description: `Routing for ${part.part_number}`,
      });
      await onRoutingChanged();
      showToast('success', 'Routing created');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create routing');
    } finally {
      setCreating(false);
    }
  };

  const openAddOp = () => {
    setEditingOp(null);
    const nextSeq = routing?.operations.length
      ? Math.max(...routing.operations.map(o => o.sequence)) + 10
      : 10;
    setOpForm({
      sequence: nextSeq, name: '', description: '', work_center_id: 0,
      setup_hours: 0, run_hours_per_unit: 0, move_hours: 0, queue_hours: 0,
      is_inspection_point: false, is_outside_operation: false,
    });
    setShowOpModal(true);
  };

  const openEditOp = (op: RoutingOperation) => {
    setEditingOp(op);
    setOpForm({
      sequence: op.sequence,
      name: op.name,
      description: op.description || '',
      work_center_id: op.work_center_id,
      setup_hours: op.setup_hours,
      run_hours_per_unit: op.run_hours_per_unit,
      move_hours: op.move_hours,
      queue_hours: op.queue_hours,
      is_inspection_point: op.is_inspection_point,
      is_outside_operation: op.is_outside_operation,
    });
    setShowOpModal(true);
  };

  const handleSubmitOp = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!routing) return;
    try {
      if (editingOp) {
        await api.updateRoutingOperation(routing.id, editingOp.id, opForm);
        showToast('success', 'Operation updated');
      } else {
        await api.addRoutingOperation(routing.id, opForm);
        showToast('success', 'Operation added');
      }
      await onRoutingChanged();
      setShowOpModal(false);
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to save operation');
    }
  };

  const handleDeleteOp = (opId: number) => {
    if (!routing) return;
    setConfirmAction({
      title: 'Delete Operation',
      message: 'Remove this operation from the routing?',
      action: async () => {
        try {
          await api.deleteRoutingOperation(routing.id, opId);
          await onRoutingChanged();
          showToast('success', 'Operation removed');
        } catch (err: any) {
          showToast('error', err.response?.data?.detail || 'Failed to delete operation');
        }
      },
    });
  };

  const handleRelease = async () => {
    if (!routing) return;
    try {
      await api.releaseRouting(routing.id);
      await onRoutingChanged();
      showToast('success', 'Routing released');
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to release routing');
    }
  };

  const handleDeleteRouting = () => {
    if (!routing) return;
    setConfirmAction({
      title: 'Delete Routing',
      message: 'This will permanently delete the routing and all its operations.',
      action: async () => {
        try {
          await api.deleteRouting(routing.id);
          await onRoutingChanged();
          showToast('success', 'Routing deleted');
        } catch (err: any) {
          showToast('error', err.response?.data?.detail || 'Failed to delete routing');
        }
      },
    });
  };

  // ── No Routing State ───────────────────────────────────────────────────

  if (!routing) {
    return (
      <div className="card text-center py-12">
        <WrenchScrewdriverIcon className="h-12 w-12 mx-auto text-gray-300 mb-4" />
        <h3 className="text-lg font-medium text-gray-900 mb-2">No Routing Defined</h3>
        <p className="text-sm text-gray-500 mb-6 max-w-md mx-auto">
          Create a routing to define the manufacturing operations, work centers, and time standards for this part.
        </p>
        <button onClick={handleCreateRouting} disabled={creating} className="btn-primary flex items-center gap-2 mx-auto">
          <PlusIcon className="h-4 w-4" />
          {creating ? 'Creating...' : 'Create Routing'}
        </button>
      </div>
    );
  }

  // ── Routing Exists ─────────────────────────────────────────────────────

  const ops = [...routing.operations].sort((a, b) => a.sequence - b.sequence);

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div className="flex items-center gap-3">
          <StatusBadge status={routing.status} />
          <span className="text-sm text-gray-500">Rev {routing.revision}</span>
          <span className="text-sm text-gray-500">{ops.length} operation{ops.length !== 1 ? 's' : ''}</span>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {routing.status === 'draft' && (
            <>
              <button onClick={openAddOp} className="btn-secondary flex items-center gap-1 text-sm">
                <PlusIcon className="h-4 w-4" />
                Add Operation
              </button>
              <button onClick={handleRelease} className="btn-success text-sm">
                <CheckCircleIcon className="h-4 w-4 mr-1 inline" />
                Release
              </button>
              <button onClick={handleDeleteRouting} className="btn-danger text-sm">
                <TrashIcon className="h-4 w-4" />
              </button>
            </>
          )}
          {routing.status === 'released' && (
            <button
              onClick={async () => {
                try {
                  // Try to unrelease - backend should support this
                  await api.updateRouting(routing.id, { status: 'draft' });
                  await onRoutingChanged();
                  showToast('success', 'Routing returned to draft');
                } catch {
                  showToast('error', 'Cannot unrelease routing');
                }
              }}
              className="btn-secondary flex items-center gap-1 text-sm"
            >
              <ArrowUturnLeftIcon className="h-4 w-4" />
              Unrelease
            </button>
          )}
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="bg-white border border-gray-200 rounded-lg p-3">
          <div className="text-xs text-gray-500 uppercase tracking-wide">Total Setup</div>
          <div className="text-lg font-semibold mt-0.5">{formatHours(routing.total_setup_hours)}</div>
        </div>
        <div className="bg-white border border-gray-200 rounded-lg p-3">
          <div className="text-xs text-gray-500 uppercase tracking-wide">Run Time/Unit</div>
          <div className="text-lg font-semibold mt-0.5">{formatHours(routing.total_run_hours_per_unit)}</div>
        </div>
        <div className="bg-white border border-gray-200 rounded-lg p-3">
          <div className="text-xs text-gray-500 uppercase tracking-wide">Labor Cost</div>
          <div className="text-lg font-semibold mt-0.5">${routing.total_labor_cost.toFixed(2)}</div>
        </div>
        <div className="bg-white border border-gray-200 rounded-lg p-3">
          <div className="text-xs text-gray-500 uppercase tracking-wide">Operations</div>
          <div className="text-lg font-semibold mt-0.5">{ops.length}</div>
        </div>
      </div>

      {/* Operations Table */}
      <div className="card overflow-hidden p-0">
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
                {routing.status === 'draft' && (
                  <th className="px-4 py-3 w-20" />
                )}
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {ops.length > 0 ? ops.map(op => (
                <tr key={op.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 text-sm font-medium">{op.operation_number}</td>
                  <td className="px-4 py-3">
                    <div className="text-sm font-medium">{op.name}</div>
                    {op.description && <div className="text-xs text-gray-400">{op.description}</div>}
                    {op.is_outside_operation && (
                      <span className="text-xs text-purple-600 font-medium">Outside Processing</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <div className="text-sm font-medium">{op.work_center?.code}</div>
                    <div className="text-xs text-gray-400">{op.work_center?.name}</div>
                  </td>
                  <td className="px-4 py-3 text-right text-sm">{formatHours(op.setup_hours)}</td>
                  <td className="px-4 py-3 text-right text-sm">{formatHours(op.run_hours_per_unit)}</td>
                  <td className="px-4 py-3 text-center">
                    {op.is_inspection_point && <CheckCircleIcon className="h-5 w-5 text-blue-500 mx-auto" />}
                  </td>
                  {routing.status === 'draft' && (
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-1">
                        <button onClick={() => openEditOp(op)} className="text-gray-400 hover:text-werco-navy-600 p-1">
                          <PencilIcon className="h-4 w-4" />
                        </button>
                        <button onClick={() => handleDeleteOp(op.id)} className="text-gray-400 hover:text-red-500 p-1">
                          <TrashIcon className="h-4 w-4" />
                        </button>
                      </div>
                    </td>
                  )}
                </tr>
              )) : (
                <tr>
                  <td colSpan={routing.status === 'draft' ? 7 : 6} className="py-12 text-center text-gray-500">
                    No operations defined yet. Add operations to define the manufacturing process.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Add/Edit Operation Modal */}
      {showOpModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setShowOpModal(false)}>
          <div className="bg-white rounded-xl p-6 max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto shadow-xl animate-scale-in" onClick={e => e.stopPropagation()}>
            <h3 className="text-lg font-semibold mb-4">
              {editingOp ? 'Edit Operation' : 'Add Operation'}
            </h3>
            <form onSubmit={handleSubmitOp} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Sequence #</label>
                  <input
                    type="number"
                    value={opForm.sequence}
                    onChange={e => setOpForm(p => ({ ...p, sequence: parseInt(e.target.value) || 0 }))}
                    className="input"
                    step={10}
                    required
                  />
                </div>
                <div>
                  <label className="label">Work Center</label>
                  <select
                    value={opForm.work_center_id}
                    onChange={e => setOpForm(p => ({ ...p, work_center_id: parseInt(e.target.value) }))}
                    className="input"
                    required
                  >
                    <option value={0}>Select...</option>
                    {workCenters.map(wc => (
                      <option key={wc.id} value={wc.id}>{wc.code} - {wc.name}</option>
                    ))}
                  </select>
                </div>
              </div>

              <div>
                <label className="label">Operation Name</label>
                <input
                  type="text"
                  value={opForm.name}
                  onChange={e => setOpForm(p => ({ ...p, name: e.target.value }))}
                  className="input"
                  placeholder="e.g., Cut to size, Weld assembly, Paint"
                  required
                />
              </div>

              <div>
                <label className="label">Description</label>
                <textarea
                  value={opForm.description}
                  onChange={e => setOpForm(p => ({ ...p, description: e.target.value }))}
                  className="input"
                  rows={2}
                />
              </div>

              {/* Time Fields */}
              <div className="grid grid-cols-2 gap-4">
                {(['setup', 'run', 'move', 'queue'] as const).map(field => {
                  const fieldKey = field === 'run' ? 'run_hours_per_unit' : `${field}_hours`;
                  const labels: Record<string, string> = {
                    setup: 'Setup Time', run: 'Run Time/Unit', move: 'Move Time', queue: 'Queue Time',
                  };
                  const hoursValue = (opForm as any)[fieldKey] as number;
                  return (
                    <div key={field}>
                      <label className="label">{labels[field]}</label>
                      <div className="flex gap-2">
                        <input
                          type="number"
                          value={toDisplay(hoursValue, timeUnits[field])}
                          onChange={e => {
                            const val = parseFloat(e.target.value) || 0;
                            setOpForm(p => ({ ...p, [fieldKey]: toHours(val, timeUnits[field]) }));
                          }}
                          className="input flex-1"
                          step={timeUnits[field] === 'min' ? 1 : 0.01}
                          min={0}
                        />
                        <select
                          value={timeUnits[field]}
                          onChange={e => setTimeUnits(p => ({ ...p, [field]: e.target.value as TimeUnit }))}
                          className="border border-gray-300 rounded-lg px-2 py-2 w-18 bg-white text-sm"
                        >
                          <option value="min">min</option>
                          <option value="hrs">hrs</option>
                        </select>
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className="flex gap-6">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={opForm.is_inspection_point}
                    onChange={e => setOpForm(p => ({ ...p, is_inspection_point: e.target.checked }))}
                    className="rounded border-gray-300 text-werco-navy-600"
                  />
                  <span className="text-sm">Inspection Point</span>
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={opForm.is_outside_operation}
                    onChange={e => setOpForm(p => ({ ...p, is_outside_operation: e.target.checked }))}
                    className="rounded border-gray-300 text-werco-navy-600"
                  />
                  <span className="text-sm">Outside Operation</span>
                </label>
              </div>

              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={() => setShowOpModal(false)} className="btn-secondary">
                  Cancel
                </button>
                <button type="submit" className="btn-primary" disabled={!opForm.name || opForm.work_center_id <= 0}>
                  {editingOp ? 'Update' : 'Add'} Operation
                </button>
              </div>
            </form>
          </div>
        </div>
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
