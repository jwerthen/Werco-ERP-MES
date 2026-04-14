import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../services/api';
import { User, WorkOrder } from '../types';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { useAuth } from '../context/AuthContext';
import { formatCentralDate, formatCentralDateTime } from '../utils/centralTime';
import {
  ArrowLeftIcon,
  PlayIcon,
  CheckCircleIcon,
  PrinterIcon,
  CubeIcon,
} from '@heroicons/react/24/outline';

const statusColors: Record<string, string> = {
  draft: 'bg-slate-800 text-slate-100',
  released: 'bg-blue-500/20 text-blue-300',
  in_progress: 'bg-green-500/20 text-green-300',
  on_hold: 'bg-yellow-500/20 text-yellow-300',
  complete: 'bg-emerald-500/20 text-emerald-300',
  closed: 'bg-slate-800 text-slate-400',
  cancelled: 'bg-red-500/20 text-red-300',
  pending: 'bg-slate-800 text-slate-100',
  ready: 'bg-blue-500/20 text-blue-300',
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

interface ActiveShopUser {
  user_id: number;
  user_name?: string;
  work_order_number?: string;
  operation?: string;
  work_center?: string;
  clock_in?: string;
  entry_type?: string;
}

const formatDateTimeCT = (value?: string) =>
  formatCentralDateTime(value, { timeZoneName: 'short' });

export default function WorkOrderDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const isAdminView = user?.role === 'admin' || !!user?.is_superuser;
  const [workOrder, setWorkOrder] = useState<WorkOrder | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [materialReqs, setMaterialReqs] = useState<MaterialRequirementsResponse | null>(null);
  const [userNameById, setUserNameById] = useState<Record<number, string>>({});
  const [activeUsersOnWorkOrder, setActiveUsersOnWorkOrder] = useState<ActiveShopUser[]>([]);
  const realtimeRefreshRef = useRef<NodeJS.Timeout | null>(null);
  const workOrderId = useMemo(() => (id ? parseInt(id, 10) : null), [id]);
  const realtimeUrl = useMemo(() => {
    if (!id) return null;
    const token = getAccessToken();
    if (!token) return null;
    return buildWsUrl(`/ws/work-order/${id}`, { token });
  }, [id]);

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

  const scheduleRealtimeRefresh = useCallback(() => {
    if (realtimeRefreshRef.current) return;
    realtimeRefreshRef.current = setTimeout(() => {
      realtimeRefreshRef.current = null;
      loadWorkOrder();
    }, 500);
  }, [loadWorkOrder]);

  useWebSocket({
    url: realtimeUrl,
    enabled: Boolean(realtimeUrl),
    onMessage: (message) => {
      if (message.type === 'connected' || message.type === 'ping') return;
      if (message.type !== 'work_order_update') return;
      const messageWorkOrderId = message.data?.work_order_id;
      if (workOrderId && messageWorkOrderId && messageWorkOrderId !== workOrderId) return;
      scheduleRealtimeRefresh();
    }
  });

  useEffect(() => {
    loadWorkOrder();
  }, [loadWorkOrder]);

  useEffect(() => {
    return () => {
      if (realtimeRefreshRef.current) {
        clearTimeout(realtimeRefreshRef.current);
        realtimeRefreshRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (!isAdminView) {
      setUserNameById({});
      return;
    }

    let cancelled = false;

    const loadUserDirectory = async () => {
      try {
        const users: User[] = await api.getUsers(true);
        if (cancelled) return;
        const lookup: Record<number, string> = {};
        users.forEach((item) => {
          const fullName = `${item.first_name || ''} ${item.last_name || ''}`.trim();
          lookup[item.id] = fullName || item.email || `User #${item.id}`;
        });
        setUserNameById(lookup);
      } catch (err) {
        if (!cancelled) {
          setUserNameById({});
        }
      }
    };

    loadUserDirectory();
    return () => {
      cancelled = true;
    };
  }, [isAdminView]);

  useEffect(() => {
    if (!isAdminView || !workOrder?.work_order_number) {
      setActiveUsersOnWorkOrder([]);
      return;
    }

    let cancelled = false;

    const loadActiveUsers = async () => {
      try {
        const response = await api.getActiveUsers();
        if (cancelled) return;
        const activeUsers: ActiveShopUser[] = Array.isArray(response?.active_users)
          ? response.active_users
          : [];
        setActiveUsersOnWorkOrder(
          activeUsers.filter((entry) => entry.work_order_number === workOrder.work_order_number)
        );
      } catch (err) {
        if (!cancelled) {
          setActiveUsersOnWorkOrder([]);
        }
      }
    };

    loadActiveUsers();

    return () => {
      cancelled = true;
    };
  }, [isAdminView, workOrder?.work_order_number, workOrder?.updated_at]);

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
      <div className="bg-red-500/10 border border-red-500/30 text-red-400 px-4 py-3 rounded-lg">
        {error || 'Work order not found'}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center">
          <button onClick={() => navigate('/work-orders')} className="mr-4 text-slate-400 hover:text-slate-300">
            <ArrowLeftIcon className="h-6 w-6" />
          </button>
          <div>
            <h1 className="text-2xl font-bold text-white">{workOrder.work_order_number}</h1>
            <p className="text-slate-400">Work Order Details</p>
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
          <h2 className="text-lg font-semibold text-white mb-4">Work Order Information</h2>
          <dl className="grid grid-cols-2 gap-4">
            <div>
              <dt className="text-sm text-slate-400">Quantity Ordered</dt>
              <dd className="text-lg font-medium">{workOrder.quantity_ordered}</dd>
            </div>
            <div>
              <dt className="text-sm text-slate-400">Quantity Complete</dt>
              <dd className="text-lg font-medium text-green-600">{workOrder.quantity_complete}</dd>
            </div>
            <div>
              <dt className="text-sm text-slate-400">Due Date</dt>
              <dd className="text-lg font-medium">
                {workOrder.due_date ? formatCentralDate(workOrder.due_date) : '-'}
              </dd>
            </div>
            <div>
              <dt className="text-sm text-slate-400">Priority</dt>
              <dd className="text-lg font-medium">{workOrder.priority}</dd>
            </div>
            <div>
              <dt className="text-sm text-slate-400">Customer</dt>
              <dd className="text-lg font-medium">{workOrder.customer_name || '-'}</dd>
            </div>
            <div>
              <dt className="text-sm text-slate-400">Customer PO</dt>
              <dd className="text-lg font-medium">{workOrder.customer_po || '-'}</dd>
            </div>
            <div>
              <dt className="text-sm text-slate-400">Actual Hours</dt>
              <dd className="text-lg font-medium">{Number(workOrder.actual_hours || 0).toFixed(2)}</dd>
            </div>
          </dl>
        </div>

        {/* Notes */}
        <div className="card">
          <h2 className="text-lg font-semibold text-white mb-4">Notes & Instructions</h2>
          <div className="space-y-4">
            <div>
              <label className="text-sm text-slate-400">Notes</label>
              <p className="mt-1">{workOrder.notes || 'No notes'}</p>
            </div>
            <div>
              <label className="text-sm text-slate-400">Special Instructions</label>
              <p className="mt-1">{workOrder.special_instructions || 'No special instructions'}</p>
            </div>
          </div>
        </div>
      </div>

      {isAdminView && (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-white">Operator Activity (Admin)</h2>
            <span className="text-xs text-slate-400">
              Live: {activeUsersOnWorkOrder.length} clocked in
            </span>
          </div>
          {activeUsersOnWorkOrder.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-700">
                <thead className="bg-slate-800/50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Operator</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Operation</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Work Center</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Entry Type</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Clocked In (CT)</th>
                  </tr>
                </thead>
                <tbody className="bg-[#151b28] divide-y divide-slate-700">
                  {activeUsersOnWorkOrder.map((entry, index) => (
                    <tr key={`${entry.user_id}-${entry.operation || 'op'}-${index}`} className="hover:bg-slate-800/50">
                      <td className="px-4 py-3 text-sm font-medium text-white">
                        {entry.user_name || userNameById[entry.user_id] || `User #${entry.user_id}`}
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-300">{entry.operation || '-'}</td>
                      <td className="px-4 py-3 text-sm text-slate-300">{entry.work_center || '-'}</td>
                      <td className="px-4 py-3 text-sm text-slate-300">
                        {entry.entry_type ? entry.entry_type.toString().replace('_', ' ') : '-'}
                      </td>
                      <td className="px-4 py-3 text-sm text-slate-300">{formatDateTimeCT(entry.clock_in)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sm text-slate-400">No one is currently clocked in on this work order.</p>
          )}
        </div>
      )}

      {/* Operations */}
      <div className="card">
        <h2 className="text-lg font-semibold text-white mb-4">Operations / Routing</h2>
        
        {workOrder.operations.length === 0 ? (
          <p className="text-slate-400">No operations defined</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-700">
              <thead className="bg-slate-800/50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Seq</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Group</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Operation</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Part</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Qty</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Est. Hours</th>
                  {isAdminView && (
                    <>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Started By</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Started At (CT)</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Completed By</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Completed At (CT)</th>
                    </>
                  )}
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Status</th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-slate-400 uppercase">Actions</th>
                </tr>
              </thead>
              <tbody className="bg-[#151b28] divide-y divide-slate-700">
                {(() => {
                  let lastGroup = '';
                  return workOrder.operations.map((op) => {
                    const isNewGroup = op.operation_group && op.operation_group !== lastGroup;
                    if (op.operation_group) lastGroup = op.operation_group;
                    
                    const groupColors: Record<string, string> = {
                      'LASER': 'bg-red-500/20 text-red-300',
                      'MACHINE': 'bg-blue-500/20 text-blue-300',
                      'BEND': 'bg-orange-500/20 text-orange-300',
                      'WELD': 'bg-yellow-500/20 text-yellow-300',
                      'FINISH': 'bg-purple-500/20 text-purple-300',
                      'ASSEMBLY': 'bg-green-500/20 text-green-300',
                      'INSPECT': 'bg-blue-500/20 text-blue-300',
                    };
                    
                    return (
                      <tr 
                        key={op.id} 
                        className={`hover:bg-slate-800/50 ${isNewGroup ? 'border-t-2 border-slate-600' : ''}`}
                      >
                        <td className="px-4 py-3 font-medium text-sm">{op.sequence}</td>
                        <td className="px-4 py-3">
                          {op.operation_group && (
                            <span className={`inline-flex px-2 py-1 rounded text-xs font-bold ${groupColors[op.operation_group] || 'bg-slate-800 text-slate-100'}`}>
                              {op.operation_group}
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <div>
                            <div className="font-medium text-sm">{op.name}</div>
                            {op.description && (
                              <div className="text-xs text-slate-400 mt-0.5">{op.description}</div>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          {op.component_part_number ? (
                            <div>
                              <div className="font-medium text-sm text-blue-600">{op.component_part_number}</div>
                              {op.component_part_name && (
                                <div className="text-xs text-slate-400">{op.component_part_name}</div>
                              )}
                            </div>
                          ) : (
                            <span className="text-slate-500 text-sm">-</span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          {op.component_quantity ? (
                            <span className="font-medium text-sm">{op.component_quantity}</span>
                          ) : (
                            <div>
                              <span className="font-medium text-sm">{op.quantity_complete}</span>
                              <span className="text-slate-400 text-sm">/{workOrder.quantity_ordered}</span>
                            </div>
                          )}
                        </td>
                        <td className="px-4 py-3 text-sm">
                          {(Number(op.setup_time_hours || 0) + Number(op.run_time_hours || 0)).toFixed(2)}
                        </td>
                        {isAdminView && (
                          <>
                            <td className="px-4 py-3 text-sm text-slate-300">
                              {op.started_by ? (userNameById[op.started_by] || `User #${op.started_by}`) : '-'}
                            </td>
                            <td className="px-4 py-3 text-sm text-slate-300">
                              {formatDateTimeCT(op.actual_start)}
                            </td>
                            <td className="px-4 py-3 text-sm text-slate-300">
                              {op.completed_by ? (userNameById[op.completed_by] || `User #${op.completed_by}`) : '-'}
                            </td>
                            <td className="px-4 py-3 text-sm text-slate-300">
                              {formatDateTimeCT(op.actual_end)}
                            </td>
                          </>
                        )}
                        <td className="px-4 py-3">
                          <span className={`inline-flex px-2 py-1 rounded-full text-xs font-medium ${statusColors[op.status]}`}>
                            {op.status.replace('_', ' ')}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-center">
                          {op.status !== 'complete' && workOrder.status !== 'draft' && (
                            <button
                              onClick={() => handleCompleteOperation(op.id, op.name)}
                              className="text-green-600 hover:text-green-300 text-sm font-medium"
                              title="Complete Operation"
                            >
                              <CheckCircleIcon className="h-5 w-5 inline" /> Complete
                            </button>
                          )}
                          {op.status === 'complete' && (
                            <span className="text-slate-500 text-sm">Done</span>
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
              <CubeIcon className="h-5 w-5 text-slate-400" />
              <h2 className="text-lg font-semibold text-white">Material Requirements</h2>
            </div>
            <span className="text-sm text-slate-400">
              BOM Rev {materialReqs.bom_revision} • Qty: {materialReqs.quantity_ordered}
            </span>
          </div>
          
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-700">
              <thead className="bg-slate-800/50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Item</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Part Number</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Description</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">Type</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Qty/Asm</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Qty Required</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Scrap</th>
                  <th className="px-4 py-3 text-right text-xs font-medium text-slate-400 uppercase">Total Needed</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase">UOM</th>
                </tr>
              </thead>
              <tbody className="bg-[#151b28] divide-y divide-slate-700">
                {materialReqs.materials.map((mat) => (
                  <tr key={mat.bom_item_id} className={mat.is_optional ? 'bg-yellow-500/10' : 'hover:bg-slate-800/50'}>
                    <td className="px-4 py-3 text-sm font-medium">{mat.item_number}</td>
                    <td className="px-4 py-3 text-sm font-medium text-blue-600">{mat.part_number}</td>
                    <td className="px-4 py-3 text-sm text-slate-300">{mat.part_name}</td>
                    <td className="px-4 py-3">
                      <span className={`text-xs px-2 py-1 rounded ${
                        mat.part_type === 'purchased' ? 'bg-green-500/20 text-green-300' :
                        mat.part_type === 'manufactured' ? 'bg-blue-500/20 text-blue-300' :
                        mat.part_type === 'raw_material' ? 'bg-yellow-500/20 text-yellow-300' :
                        'bg-slate-800 text-slate-100'
                      }`}>
                        {mat.part_type.replace('_', ' ')}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-sm text-right">{mat.quantity_per_assembly}</td>
                    <td className="px-4 py-3 text-sm text-right font-medium">{mat.quantity_required}</td>
                    <td className="px-4 py-3 text-sm text-right text-slate-400">
                      {mat.scrap_allowance > 0 ? `+${mat.scrap_allowance}` : '-'}
                    </td>
                    <td className="px-4 py-3 text-sm text-right font-bold text-green-400">{mat.total_required}</td>
                    <td className="px-4 py-3 text-sm text-slate-400">{mat.unit_of_measure}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          
          <div className="mt-4 text-sm text-slate-400">
            <span className="bg-yellow-500/10 px-2 py-1 rounded">Optional items</span> highlighted in yellow
          </div>
        </div>
      )}
      
      {materialReqs && !materialReqs.has_bom && (
        <div className="card">
          <div className="flex items-center gap-2 text-slate-400">
            <CubeIcon className="h-5 w-5" />
            <span>No BOM defined for this part</span>
          </div>
        </div>
      )}
    </div>
  );
}
