import React, { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../services/api';
import { WorkOrder } from '../types';
import { format } from 'date-fns';
import {
  ArrowLeftIcon,
  PlayIcon,
  CheckCircleIcon,
  PrinterIcon,
} from '@heroicons/react/24/outline';

const statusColors: Record<string, string> = {
  draft: 'bg-gray-100 text-gray-800',
  released: 'bg-blue-100 text-blue-800',
  in_progress: 'bg-green-100 text-green-800',
  on_hold: 'bg-yellow-100 text-yellow-800',
  complete: 'bg-emerald-100 text-emerald-800',
  closed: 'bg-gray-100 text-gray-600',
  cancelled: 'bg-red-100 text-red-800',
  pending: 'bg-gray-100 text-gray-800',
  ready: 'bg-blue-100 text-blue-800',
};

export default function WorkOrderDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [workOrder, setWorkOrder] = useState<WorkOrder | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    loadWorkOrder();
  }, [id]);

  const loadWorkOrder = async () => {
    try {
      const response = await api.getWorkOrder(parseInt(id!));
      setWorkOrder(response);
    } catch (err) {
      setError('Failed to load work order');
    } finally {
      setLoading(false);
    }
  };

  const handleRelease = async () => {
    try {
      await api.releaseWorkOrder(workOrder!.id);
      loadWorkOrder();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to release work order');
    }
  };

  const handleStart = async () => {
    try {
      await api.startWorkOrder(workOrder!.id);
      loadWorkOrder();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to start work order');
    }
  };

  const handleComplete = async () => {
    const qtyComplete = prompt(`Enter quantity completed (ordered: ${workOrder!.quantity_ordered}):`, workOrder!.quantity_ordered.toString());
    if (!qtyComplete) return;
    
    const qtyScrapped = prompt('Enter quantity scrapped (if any):', '0');
    
    try {
      await api.completeWorkOrder(workOrder!.id, parseFloat(qtyComplete), parseFloat(qtyScrapped || '0'));
      loadWorkOrder();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to complete work order');
    }
  };

  const handleCompleteOperation = async (operationId: number, opName: string) => {
    const qtyComplete = prompt(`Complete operation "${opName}"\nEnter quantity completed:`, workOrder!.quantity_ordered.toString());
    if (!qtyComplete) return;
    
    const qtyScrapped = prompt('Enter quantity scrapped (if any):', '0');
    
    try {
      await api.completeOperation(operationId, parseFloat(qtyComplete), parseFloat(qtyScrapped || '0'));
      loadWorkOrder();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to complete operation');
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  if (error || !workOrder) {
    return (
      <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">
        {error || 'Work order not found'}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center">
          <button onClick={() => navigate('/work-orders')} className="mr-4 text-gray-500 hover:text-gray-700">
            <ArrowLeftIcon className="h-6 w-6" />
          </button>
          <div>
            <h1 className="text-2xl font-bold text-gray-900">{workOrder.work_order_number}</h1>
            <p className="text-gray-500">Work Order Details</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className={`px-3 py-1 rounded-full text-sm font-medium ${statusColors[workOrder.status]}`}>
            {workOrder.status.replace('_', ' ')}
          </span>
          {workOrder.status === 'draft' && (
            <button onClick={handleRelease} className="btn-primary flex items-center">
              <PlayIcon className="h-5 w-5 mr-2" />
              Release
            </button>
          )}
          {workOrder.status === 'released' && (
            <button onClick={handleStart} className="btn-success flex items-center">
              <PlayIcon className="h-5 w-5 mr-2" />
              Start
            </button>
          )}
          {workOrder.status === 'in_progress' && (
            <button onClick={handleComplete} className="btn-primary flex items-center">
              <CheckCircleIcon className="h-5 w-5 mr-2" />
              Complete
            </button>
          )}
          <button 
            onClick={() => window.open(`/print/traveler/${workOrder.id}`, '_blank')}
            className="btn-secondary flex items-center"
          >
            <PrinterIcon className="h-5 w-5 mr-2" />
            Print Traveler
          </button>
        </div>
      </div>

      {/* Details Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Work Order Info */}
        <div className="card">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">Work Order Information</h2>
          <dl className="grid grid-cols-2 gap-4">
            <div>
              <dt className="text-sm text-gray-500">Quantity Ordered</dt>
              <dd className="text-lg font-medium">{workOrder.quantity_ordered}</dd>
            </div>
            <div>
              <dt className="text-sm text-gray-500">Quantity Complete</dt>
              <dd className="text-lg font-medium text-green-600">{workOrder.quantity_complete}</dd>
            </div>
            <div>
              <dt className="text-sm text-gray-500">Due Date</dt>
              <dd className="text-lg font-medium">
                {workOrder.due_date ? format(new Date(workOrder.due_date), 'MMM d, yyyy') : '-'}
              </dd>
            </div>
            <div>
              <dt className="text-sm text-gray-500">Priority</dt>
              <dd className="text-lg font-medium">{workOrder.priority}</dd>
            </div>
            <div>
              <dt className="text-sm text-gray-500">Customer</dt>
              <dd className="text-lg font-medium">{workOrder.customer_name || '-'}</dd>
            </div>
            <div>
              <dt className="text-sm text-gray-500">Customer PO</dt>
              <dd className="text-lg font-medium">{workOrder.customer_po || '-'}</dd>
            </div>
            <div>
              <dt className="text-sm text-gray-500">Lot Number</dt>
              <dd className="text-lg font-medium">{workOrder.lot_number || '-'}</dd>
            </div>
            <div>
              <dt className="text-sm text-gray-500">Actual Hours</dt>
              <dd className="text-lg font-medium">{workOrder.actual_hours.toFixed(2)}</dd>
            </div>
          </dl>
        </div>

        {/* Notes */}
        <div className="card">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">Notes & Instructions</h2>
          <div className="space-y-4">
            <div>
              <label className="text-sm text-gray-500">Notes</label>
              <p className="mt-1">{workOrder.notes || 'No notes'}</p>
            </div>
            <div>
              <label className="text-sm text-gray-500">Special Instructions</label>
              <p className="mt-1">{workOrder.special_instructions || 'No special instructions'}</p>
            </div>
          </div>
        </div>
      </div>

      {/* Operations */}
      <div className="card">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">Operations / Routing</h2>
        
        {workOrder.operations.length === 0 ? (
          <p className="text-gray-500">No operations defined</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Seq</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Operation</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Work Center</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Est. Hours</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Actual Hours</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Qty Complete</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {workOrder.operations.map((op) => (
                  <tr key={op.id} className="hover:bg-gray-50">
                    <td className="px-4 py-4 font-medium">{op.sequence}</td>
                    <td className="px-4 py-4">
                      <div>
                        <div className="font-medium">{op.operation_number || `OP${op.sequence}`}</div>
                        <div className="text-sm text-gray-500">{op.name}</div>
                      </div>
                    </td>
                    <td className="px-4 py-4 text-sm">{op.work_center_id}</td>
                    <td className="px-4 py-4 text-sm">
                      {(op.setup_time_hours + op.run_time_hours).toFixed(2)}
                    </td>
                    <td className="px-4 py-4 text-sm">
                      {(op.actual_setup_hours + op.actual_run_hours).toFixed(2)}
                    </td>
                    <td className="px-4 py-4">
                      <span className="font-medium">{op.quantity_complete}</span>
                      <span className="text-gray-500">/{workOrder.quantity_ordered}</span>
                    </td>
                    <td className="px-4 py-4">
                      <span className={`inline-flex px-2 py-1 rounded-full text-xs font-medium ${statusColors[op.status]}`}>
                        {op.status.replace('_', ' ')}
                      </span>
                    </td>
                    <td className="px-4 py-4 text-center">
                      {op.status !== 'complete' && workOrder.status !== 'draft' && (
                        <button
                          onClick={() => handleCompleteOperation(op.id, op.name)}
                          className="text-green-600 hover:text-green-800 text-sm font-medium"
                          title="Complete Operation"
                        >
                          <CheckCircleIcon className="h-5 w-5 inline" /> Complete
                        </button>
                      )}
                      {op.status === 'complete' && (
                        <span className="text-gray-400 text-sm">Done</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
