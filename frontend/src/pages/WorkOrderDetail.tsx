import React, { useEffect, useState, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../services/api';
import { WorkOrder } from '../types';
import { format } from 'date-fns';
import {
  ArrowLeftIcon,
  PlayIcon,
  CheckCircleIcon,
  PrinterIcon,
  CubeIcon,
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

interface MaterialRequirement {
  bom_item_id: number;
  item_number: number;
  part_id: number;
  part_number: string;
  part_name: string;
  part_type: string;
  quantity_per_assembly: number;
  quantity_required: number;
  scrap_factor: number;
  scrap_allowance: number;
  total_required: number;
  unit_of_measure: string;
  item_type: string;
  is_optional: boolean;
  notes: string | null;
}

interface MaterialRequirementsResponse {
  work_order_id: number;
  work_order_number: string;
  quantity_ordered: number;
  has_bom: boolean;
  bom_id?: number;
  bom_revision?: string;
  materials: MaterialRequirement[];
}

export default function WorkOrderDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [workOrder, setWorkOrder] = useState<WorkOrder | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [materialReqs, setMaterialReqs] = useState<MaterialRequirementsResponse | null>(null);

  const loadWorkOrder = useCallback(async () => {
    try {
      const response = await api.getWorkOrder(parseInt(id!));
      setWorkOrder(response);
      
      // Load material requirements
      try {
        const matReqs = await api.getMaterialRequirements(parseInt(id!));
        setMaterialReqs(matReqs);
      } catch (e) {
        // Material requirements may not exist for all parts
      }
    } catch (err) {
      setError('Failed to load work order');
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    loadWorkOrder();
  }, [loadWorkOrder]);

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
      await api.completeWOOperation(operationId, parseFloat(qtyComplete), parseFloat(qtyScrapped || '0'));
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
            onClick={() => window.open(`/print/traveler/${workOrder.id}?autoprint=1`, '_blank')}
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
              <dd className="text-lg font-medium">{Number(workOrder.actual_hours || 0).toFixed(2)}</dd>
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
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Group</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Operation</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Qty</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Est. Hours</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {(() => {
                  let lastGroup = '';
                  return workOrder.operations.map((op) => {
                    const isNewGroup = op.operation_group && op.operation_group !== lastGroup;
                    if (op.operation_group) lastGroup = op.operation_group;
                    
                    const groupColors: Record<string, string> = {
                      'LASER': 'bg-red-100 text-red-800',
                      'MACHINE': 'bg-blue-100 text-blue-800',
                      'BEND': 'bg-orange-100 text-orange-800',
                      'WELD': 'bg-yellow-100 text-yellow-800',
                      'FINISH': 'bg-purple-100 text-purple-800',
                      'ASSEMBLY': 'bg-green-100 text-green-800',
                      'INSPECT': 'bg-cyan-100 text-cyan-800',
                    };
                    
                    return (
                      <tr 
                        key={op.id} 
                        className={`hover:bg-gray-50 ${isNewGroup ? 'border-t-2 border-gray-300' : ''}`}
                      >
                        <td className="px-4 py-3 font-medium text-sm">{op.sequence}</td>
                        <td className="px-4 py-3">
                          {op.operation_group && (
                            <span className={`inline-flex px-2 py-1 rounded text-xs font-bold ${groupColors[op.operation_group] || 'bg-gray-100 text-gray-800'}`}>
                              {op.operation_group}
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <div>
                            <div className="font-medium text-sm">{op.name}</div>
                            {op.description && (
                              <div className="text-xs text-gray-500 mt-0.5">{op.description}</div>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          {op.component_part_number ? (
                            <div>
                              <div className="font-medium text-sm text-blue-600">{op.component_part_number}</div>
                              {op.component_part_name && (
                                <div className="text-xs text-gray-500">{op.component_part_name}</div>
                              )}
                            </div>
                          ) : (
                            <span className="text-gray-400 text-sm">-</span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          {op.component_quantity ? (
                            <span className="font-medium text-sm">{op.component_quantity}</span>
                          ) : (
                            <div>
                              <span className="font-medium text-sm">{op.quantity_complete}</span>
                              <span className="text-gray-500 text-sm">/{workOrder.quantity_ordered}</span>
                            </div>
                          )}
                        </td>
                        <td className="px-4 py-3 text-sm">
                          {(Number(op.setup_time_hours || 0) + Number(op.run_time_hours || 0)).toFixed(2)}
                        </td>
                        <td className="px-4 py-3">
                          <span className={`inline-flex px-2 py-1 rounded-full text-xs font-medium ${statusColors[op.status]}`}>
                            {op.status.replace('_', ' ')}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-center">
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
                    );
                  });
                })()}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Material Requirements */}
      {materialReqs && materialReqs.has_bom && materialReqs.materials.length > 0 && (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <CubeIcon className="h-5 w-5 text-gray-500" />
              <h2 className="text-lg font-semibold text-gray-900">Material Requirements</h2>
            </div>
            <span className="text-sm text-gray-500">
              BOM Rev {materialReqs.bom_revision} • Qty: {materialReqs.quantity_ordered}
            </span>
          </div>
          
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Item</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part Number</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Description</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Qty/Asm</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Qty Required</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Scrap</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Total Needed</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">UOM</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {materialReqs.materials.map((mat) => (
                  <tr key={mat.bom_item_id} className={mat.is_optional ? 'bg-yellow-50' : 'hover:bg-gray-50'}>
                    <td className="px-4 py-3 text-sm font-medium">{mat.item_number}</td>
                    <td className="px-4 py-3 text-sm font-medium text-blue-600">{mat.part_number}</td>
                    <td className="px-4 py-3 text-sm text-gray-700">{mat.part_name}</td>
                    <td className="px-4 py-3">
                      <span className={`text-xs px-2 py-1 rounded ${
                        mat.part_type === 'purchased' ? 'bg-green-100 text-green-800' :
                        mat.part_type === 'manufactured' ? 'bg-blue-100 text-blue-800' :
                        mat.part_type === 'raw_material' ? 'bg-yellow-100 text-yellow-800' :
                        'bg-gray-100 text-gray-800'
                      }`}>
                        {mat.part_type.replace('_', ' ')}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-sm text-right">{mat.quantity_per_assembly}</td>
                    <td className="px-4 py-3 text-sm text-right font-medium">{mat.quantity_required}</td>
                    <td className="px-4 py-3 text-sm text-right text-gray-500">
                      {mat.scrap_allowance > 0 ? `+${mat.scrap_allowance}` : '-'}
                    </td>
                    <td className="px-4 py-3 text-sm text-right font-bold text-green-700">{mat.total_required}</td>
                    <td className="px-4 py-3 text-sm text-gray-500">{mat.unit_of_measure}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          
          <div className="mt-4 text-sm text-gray-500">
            <span className="bg-yellow-50 px-2 py-1 rounded">Optional items</span> highlighted in yellow
          </div>
        </div>
      )}
      
      {materialReqs && !materialReqs.has_bom && (
        <div className="card">
          <div className="flex items-center gap-2 text-gray-500">
            <CubeIcon className="h-5 w-5" />
            <span>No BOM defined for this part</span>
          </div>
        </div>
      )}
    </div>
  );
}
