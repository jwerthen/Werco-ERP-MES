import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import {
  InformationCircleIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  PlusIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';

interface Part {
  id: number;
  part_number: string;
  name: string;
  part_type: string;
}

interface WorkCenter {
  id: number;
  code: string;
  name: string;
}

interface RoutingOperation {
  id: number;
  sequence: number;
  operation_number: string;
  name: string;
  description?: string;
  work_center_id: number;
  work_center?: { id: number; code: string; name: string };
  setup_hours: number;
  run_hours_per_unit: number;
  work_instructions?: string;
}

interface Routing {
  id: number;
  part_id: number;
  revision: string;
  status: string;
  operations: RoutingOperation[];
}

interface OperationPreview {
  sequence: number;
  operation_number: string;
  name: string;
  work_center_id: number;
  work_center_name: string;
  setup_time_hours: number;
  run_time_hours: number;
  fromRouting: boolean;
}

export default function WorkOrderNew() {
  const navigate = useNavigate();
  const [parts, setParts] = useState<Part[]>([]);
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [loadingRouting, setLoadingRouting] = useState(false);
  const [routing, setRouting] = useState<Routing | null>(null);
  const [operations, setOperations] = useState<OperationPreview[]>([]);
  const [showManualEntry, setShowManualEntry] = useState(false);

  const [form, setForm] = useState({
    part_id: 0,
    quantity_ordered: 1,
    priority: 5,
    customer_name: '',
    customer_po: '',
    due_date: '',
    lot_number: '',
    notes: ''
  });

  useEffect(() => {
    loadInitialData();
  }, []);

  const loadInitialData = async () => {
    try {
      const [partsRes, wcRes] = await Promise.all([
        api.getParts({ active_only: true }),
        api.getWorkCenters()
      ]);
      setParts(partsRes);
      setWorkCenters(wcRes);
    } catch (err) {
      console.error('Failed to load data:', err);
    } finally {
      setLoading(false);
    }
  };

  const handlePartChange = async (partId: number) => {
    setForm({ ...form, part_id: partId });
    setRouting(null);
    setOperations([]);
    setShowManualEntry(false);

    if (!partId) return;

    setLoadingRouting(true);
    try {
      const routingRes = await api.getRoutingByPart(partId);
      if (routingRes && routingRes.operations?.length > 0) {
        setRouting(routingRes);
        const ops: OperationPreview[] = routingRes.operations
          .filter((op: RoutingOperation) => op.work_center)
          .map((op: RoutingOperation) => ({
            sequence: op.sequence,
            operation_number: op.operation_number || `Op ${op.sequence}`,
            name: op.name,
            work_center_id: op.work_center_id,
            work_center_name: op.work_center?.name || '',
            setup_time_hours: op.setup_hours,
            run_time_hours: op.run_hours_per_unit * form.quantity_ordered,
            fromRouting: true
          }));
        setOperations(ops);
      } else {
        setShowManualEntry(true);
      }
    } catch (err) {
      console.error('Failed to load routing:', err);
      setShowManualEntry(true);
    } finally {
      setLoadingRouting(false);
    }
  };

  const handleQuantityChange = (qty: number) => {
    setForm({ ...form, quantity_ordered: qty });
    if (routing) {
      setOperations(ops => ops.map(op => ({
        ...op,
        run_time_hours: op.fromRouting 
          ? (routing.operations.find(r => r.sequence === op.sequence)?.run_hours_per_unit || 0) * qty
          : op.run_time_hours
      })));
    }
  };

  const updateOperation = (index: number, field: keyof OperationPreview, value: any) => {
    setOperations(ops => {
      const updated = [...ops];
      updated[index] = { ...updated[index], [field]: value, fromRouting: false };
      if (field === 'work_center_id') {
        const wc = workCenters.find(w => w.id === value);
        updated[index].work_center_name = wc?.name || '';
      }
      return updated;
    });
  };

  const addManualOperation = () => {
    const nextSeq = operations.length > 0 
      ? Math.max(...operations.map(o => o.sequence)) + 10 
      : 10;
    setOperations([...operations, {
      sequence: nextSeq,
      operation_number: `Op ${nextSeq}`,
      name: '',
      work_center_id: workCenters[0]?.id || 0,
      work_center_name: workCenters[0]?.name || '',
      setup_time_hours: 0,
      run_time_hours: 0,
      fromRouting: false
    }]);
  };

  const removeOperation = (index: number) => {
    setOperations(ops => ops.filter((_, i) => i !== index));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.part_id) {
      alert('Please select a part');
      return;
    }

    setSubmitting(true);
    try {
      const payload: any = {
        ...form,
        due_date: form.due_date || null,
      };

      // If operations were modified or manually entered, include them
      const hasModifiedOps = operations.some(op => !op.fromRouting);
      if (hasModifiedOps || showManualEntry) {
        payload.operations = operations.map(op => ({
          sequence: op.sequence,
          operation_number: op.operation_number,
          name: op.name,
          work_center_id: op.work_center_id,
          setup_time_hours: op.setup_time_hours,
          run_time_hours: op.run_time_hours,
          status: 'pending'
        }));
      } else {
        payload.operations = [];
      }

      const result = await api.createWorkOrder(payload);
      navigate(`/work-orders/${result.id}`);
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to create work order');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="spinner h-12 w-12"></div>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto">
      <h1 className="text-2xl font-bold text-surface-900 mb-6">New Work Order</h1>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Basic Info Card */}
        <div className="card">
          <h2 className="text-lg font-semibold text-surface-900 mb-4">Work Order Details</h2>
          
          <div className="space-y-4">
            <div>
              <label className="label">Part *</label>
              <select
                value={form.part_id}
                onChange={(e) => handlePartChange(parseInt(e.target.value))}
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

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="label">Quantity *</label>
                <input
                  type="number"
                  value={form.quantity_ordered}
                  onChange={(e) => handleQuantityChange(parseInt(e.target.value) || 1)}
                  className="input"
                  min={1}
                  required
                />
              </div>
              <div>
                <label className="label">Priority</label>
                <select
                  value={form.priority}
                  onChange={(e) => setForm({ ...form, priority: parseInt(e.target.value) })}
                  className="input"
                >
                  <option value={1}>1 - Critical</option>
                  <option value={2}>2 - Urgent</option>
                  <option value={3}>3 - High</option>
                  <option value={5}>5 - Normal</option>
                  <option value={7}>7 - Low</option>
                  <option value={10}>10 - Lowest</option>
                </select>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="label">Customer Name</label>
                <input
                  type="text"
                  value={form.customer_name}
                  onChange={(e) => setForm({ ...form, customer_name: e.target.value })}
                  className="input"
                />
              </div>
              <div>
                <label className="label">Customer PO #</label>
                <input
                  type="text"
                  value={form.customer_po}
                  onChange={(e) => setForm({ ...form, customer_po: e.target.value })}
                  className="input"
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="label">Due Date</label>
                <input
                  type="date"
                  value={form.due_date}
                  onChange={(e) => setForm({ ...form, due_date: e.target.value })}
                  className="input"
                />
              </div>
              <div>
                <label className="label">Lot Number</label>
                <input
                  type="text"
                  value={form.lot_number}
                  onChange={(e) => setForm({ ...form, lot_number: e.target.value })}
                  className="input"
                  placeholder="Auto-generated if blank"
                />
              </div>
            </div>

            <div>
              <label className="label">Notes</label>
              <textarea
                value={form.notes}
                onChange={(e) => setForm({ ...form, notes: e.target.value })}
                className="input"
                rows={2}
              />
            </div>
          </div>
        </div>

        {/* Operations Card */}
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-surface-900">Operations</h2>
            {operations.length > 0 && (
              <button
                type="button"
                onClick={addManualOperation}
                className="btn-secondary btn-sm"
              >
                <PlusIcon className="h-4 w-4 mr-1" />
                Add Operation
              </button>
            )}
          </div>

          {loadingRouting && (
            <div className="flex items-center justify-center py-8">
              <div className="spinner h-8 w-8"></div>
              <span className="ml-3 text-surface-500">Loading routing...</span>
            </div>
          )}

          {!loadingRouting && form.part_id === 0 && (
            <div className="flex items-center gap-3 p-4 bg-surface-50 rounded-xl text-surface-500">
              <InformationCircleIcon className="h-5 w-5 flex-shrink-0" />
              <span>Select a part to see available operations</span>
            </div>
          )}

          {!loadingRouting && form.part_id > 0 && routing && operations.length > 0 && (
            <>
              <div className="flex items-center gap-2 mb-4 p-3 bg-emerald-50 border border-emerald-200 rounded-xl text-emerald-700">
                <CheckCircleIcon className="h-5 w-5 flex-shrink-0" />
                <span className="text-sm font-medium">
                  Auto-populated from routing Rev {routing.revision} ({operations.length} operations)
                </span>
              </div>
              
              <div className="overflow-x-auto">
                <table className="table">
                  <thead>
                    <tr>
                      <th className="w-20">Seq</th>
                      <th>Operation</th>
                      <th>Work Center</th>
                      <th className="w-28">Setup (hr)</th>
                      <th className="w-28">Run (hr)</th>
                      <th className="w-16"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {operations.map((op, index) => (
                      <tr key={index} className={!op.fromRouting ? 'bg-amber-50' : ''}>
                        <td>
                          <input
                            type="number"
                            value={op.sequence}
                            onChange={(e) => updateOperation(index, 'sequence', parseInt(e.target.value) || 0)}
                            className="input input-sm w-16 text-center"
                          />
                        </td>
                        <td>
                          <input
                            type="text"
                            value={op.name}
                            onChange={(e) => updateOperation(index, 'name', e.target.value)}
                            className="input input-sm"
                            placeholder="Operation name"
                          />
                        </td>
                        <td>
                          <select
                            value={op.work_center_id}
                            onChange={(e) => updateOperation(index, 'work_center_id', parseInt(e.target.value))}
                            className="input input-sm"
                          >
                            {workCenters.map(wc => (
                              <option key={wc.id} value={wc.id}>{wc.name}</option>
                            ))}
                          </select>
                        </td>
                        <td>
                          <input
                            type="number"
                            step="0.01"
                            value={op.setup_time_hours}
                            onChange={(e) => updateOperation(index, 'setup_time_hours', parseFloat(e.target.value) || 0)}
                            className="input input-sm text-right"
                          />
                        </td>
                        <td>
                          <input
                            type="number"
                            step="0.01"
                            value={op.run_time_hours.toFixed(2)}
                            onChange={(e) => updateOperation(index, 'run_time_hours', parseFloat(e.target.value) || 0)}
                            className="input input-sm text-right"
                          />
                        </td>
                        <td>
                          <button
                            type="button"
                            onClick={() => removeOperation(index)}
                            className="p-1.5 rounded-lg text-surface-400 hover:text-red-600 hover:bg-red-50"
                          >
                            <TrashIcon className="h-4 w-4" />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              
              {operations.some(op => !op.fromRouting) && (
                <p className="text-xs text-amber-600 mt-2">
                  * Yellow rows have been modified from the original routing
                </p>
              )}
            </>
          )}

          {!loadingRouting && form.part_id > 0 && !routing && (
            <>
              <div className="flex items-center gap-2 mb-4 p-3 bg-amber-50 border border-amber-200 rounded-xl text-amber-700">
                <ExclamationTriangleIcon className="h-5 w-5 flex-shrink-0" />
                <span className="text-sm">
                  No released routing found for this part. Add operations manually.
                </span>
              </div>

              {operations.length === 0 ? (
                <button
                  type="button"
                  onClick={addManualOperation}
                  className="w-full py-8 border-2 border-dashed border-surface-300 rounded-xl text-surface-500 hover:border-werco-400 hover:text-werco-600 transition-colors"
                >
                  <PlusIcon className="h-6 w-6 mx-auto mb-2" />
                  Add First Operation
                </button>
              ) : (
                <div className="overflow-x-auto">
                  <table className="table">
                    <thead>
                      <tr>
                        <th className="w-20">Seq</th>
                        <th>Operation</th>
                        <th>Work Center</th>
                        <th className="w-28">Setup (hr)</th>
                        <th className="w-28">Run (hr)</th>
                        <th className="w-16"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {operations.map((op, index) => (
                        <tr key={index}>
                          <td>
                            <input
                              type="number"
                              value={op.sequence}
                              onChange={(e) => updateOperation(index, 'sequence', parseInt(e.target.value) || 0)}
                              className="input input-sm w-16 text-center"
                            />
                          </td>
                          <td>
                            <input
                              type="text"
                              value={op.name}
                              onChange={(e) => updateOperation(index, 'name', e.target.value)}
                              className="input input-sm"
                              placeholder="Operation name"
                              required
                            />
                          </td>
                          <td>
                            <select
                              value={op.work_center_id}
                              onChange={(e) => updateOperation(index, 'work_center_id', parseInt(e.target.value))}
                              className="input input-sm"
                            >
                              {workCenters.map(wc => (
                                <option key={wc.id} value={wc.id}>{wc.name}</option>
                              ))}
                            </select>
                          </td>
                          <td>
                            <input
                              type="number"
                              step="0.01"
                              value={op.setup_time_hours}
                              onChange={(e) => updateOperation(index, 'setup_time_hours', parseFloat(e.target.value) || 0)}
                              className="input input-sm text-right"
                            />
                          </td>
                          <td>
                            <input
                              type="number"
                              step="0.01"
                              value={op.run_time_hours}
                              onChange={(e) => updateOperation(index, 'run_time_hours', parseFloat(e.target.value) || 0)}
                              className="input input-sm text-right"
                            />
                          </td>
                          <td>
                            <button
                              type="button"
                              onClick={() => removeOperation(index)}
                              className="p-1.5 rounded-lg text-surface-400 hover:text-red-600 hover:bg-red-50"
                            >
                              <TrashIcon className="h-4 w-4" />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>

        {/* Actions */}
        <div className="flex justify-end gap-3">
          <button
            type="button"
            onClick={() => navigate('/work-orders')}
            className="btn-secondary"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitting || !form.part_id}
            className="btn-primary"
          >
            {submitting ? 'Creating...' : 'Create Work Order'}
          </button>
        </div>
      </form>
    </div>
  );
}
