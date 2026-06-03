import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import api from '../services/api';
import { User, WorkOrder, WorkOrderOperation } from '../types';
import { WorkOrderBlocker, WorkOrderBlockerCategory, WorkOrderBlockerSeverity } from '../types/aiForward';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { useAuth } from '../context/AuthContext';
import { formatCentralDate, formatCentralDateTime } from '../utils/centralTime';
import {
  ArrowLeftIcon,
  PlayIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  PrinterIcon,
  CubeIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';

const CURRENT_WORK_ORDER_STATUSES = ['released', 'in_progress', 'on_hold'];

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

const operationProgressKey = (op: WorkOrderOperation) => {
  if (op.sequence !== undefined && op.sequence !== null) {
    return `sequence|${Number(op.sequence)}`;
  }
  const operationNumber = String(op.operation_number || '').replace(/\D/g, '');
  if (operationNumber) {
    return `operation_number|${operationNumber}`;
  }
  const name = (op.name || '').trim().toLowerCase().replace(/\s+/g, ' ');
  return [
    op.work_center_id || '',
    op.component_part_id || '',
    op.operation_group || '',
    name || op.operation_number || op.sequence || op.id,
  ].join('|');
};

const getOperationProgressMetrics = (workOrder: WorkOrder) => {
  const operations = workOrder.operations || [];
  if (operations.length === 0) {
    const ordered = Number(workOrder.quantity_ordered || 0);
    const complete = Number(workOrder.quantity_complete || 0);
    return {
      operation_count: 0,
      operations_complete: 0,
      percent: ordered > 0 ? Math.min(100, Math.max(0, (complete / ordered) * 100)) : 0,
      label: `${complete}/${ordered}`,
    };
  }

  const progressByKey = new Map<string, number>();
  const completeByKey = new Map<string, boolean>();
  operations.forEach((op) => {
    const target = Number(op.component_quantity || workOrder.quantity_ordered || 0);
    const complete = Number(op.quantity_complete || 0);
    const hasCompletionEvidence = op.status === 'complete' || Boolean(op.actual_end && op.completed_by);
    const ratio = hasCompletionEvidence
      ? 1
      : target > 0
        ? Math.min(1, Math.max(0, complete / target))
        : 0;
    const key = operationProgressKey(op);
    progressByKey.set(key, Math.max(progressByKey.get(key) || 0, ratio));
    completeByKey.set(key, Boolean(completeByKey.get(key)) || hasCompletionEvidence);
  });

  const operationCount = progressByKey.size;
  const operationsComplete = Array.from(completeByKey.values()).filter(Boolean).length;
  const progressTotal = Array.from(progressByKey.values()).reduce((sum, ratio) => sum + ratio, 0);
  const percent = operationCount > 0 ? Math.round((progressTotal / operationCount) * 1000) / 10 : 0;

  return {
    operation_count: operationCount,
    operations_complete: operationsComplete,
    percent,
    label: `${operationsComplete}/${operationCount} ops`,
  };
};

const syncOperationProgressSummary = (workOrder: WorkOrder): WorkOrder => {
  const metrics = getOperationProgressMetrics(workOrder);
  return {
    ...workOrder,
    operation_count: metrics.operation_count,
    operations_complete: metrics.operations_complete,
    operation_progress_percent: metrics.percent,
  };
};

const getDetailWorkOrderProgress = (workOrder: WorkOrder) => getOperationProgressMetrics(workOrder);

const hydrateOperationsFromShopFloor = async (workOrder: WorkOrder): Promise<WorkOrder> => {
  const firstOperationId = workOrder.operations?.[0]?.id;
  if (!firstOperationId) return syncOperationProgressSummary(workOrder);

  try {
    const details = await api.getOperationDetails(firstOperationId);
    const liveOperations = Array.isArray(details?.all_operations) ? details.all_operations : [];
    if (liveOperations.length === 0) return syncOperationProgressSummary(workOrder);

    const liveById = new Map<number, Partial<WorkOrderOperation>>(
      liveOperations.map((op: Partial<WorkOrderOperation> & { id: number }) => [op.id, op])
    );
    return syncOperationProgressSummary({
      ...workOrder,
      operations: workOrder.operations.map((op) => {
        const liveOp = liveById.get(op.id);
        if (!liveOp) return op;

        return {
          ...op,
          status: liveOp.status ?? op.status,
          quantity_complete: liveOp.quantity_complete ?? op.quantity_complete,
          quantity_scrapped: liveOp.quantity_scrapped ?? op.quantity_scrapped,
          actual_setup_hours: liveOp.actual_setup_hours ?? op.actual_setup_hours,
          actual_run_hours: liveOp.actual_run_hours ?? op.actual_run_hours,
          actual_start: liveOp.actual_start ?? op.actual_start,
          actual_end: liveOp.actual_end ?? op.actual_end,
          started_by: liveOp.started_by ?? op.started_by,
          completed_by: liveOp.completed_by ?? op.completed_by,
        };
      }),
    });
  } catch {
    return syncOperationProgressSummary(workOrder);
  }
};

export default function WorkOrderDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const isAdminView = user?.role === 'admin' || !!user?.is_superuser;
  const [workOrder, setWorkOrder] = useState<WorkOrder | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [materialReqs, setMaterialReqs] = useState<MaterialRequirementsResponse | null>(null);
  const [blockers, setBlockers] = useState<WorkOrderBlocker[]>([]);
  const [blockerForm, setBlockerForm] = useState<{
    operation_id: string;
    category: WorkOrderBlockerCategory;
    severity: WorkOrderBlockerSeverity;
    note: string;
  }>({
    operation_id: '',
    category: 'material_missing',
    severity: 'high',
    note: '',
  });
  const [submittingBlocker, setSubmittingBlocker] = useState(false);
  const [resolvingBlockerId, setResolvingBlockerId] = useState<number | null>(null);
  const [userNameById, setUserNameById] = useState<Record<number, string>>({});
  const [activeUsersOnWorkOrder, setActiveUsersOnWorkOrder] = useState<ActiveShopUser[]>([]);
  const realtimeRefreshRef = useRef<NodeJS.Timeout | null>(null);
  const loadRequestRef = useRef(0);
  const workOrderId = useMemo(() => (id ? parseInt(id, 10) : null), [id]);
  const realtimeUrl = useMemo(() => {
    if (!id) return null;
    const token = getAccessToken();
    if (!token) return null;
    return buildWsUrl(`/ws/work-order/${id}`, { token });
  }, [id]);

  const loadWorkOrder = useCallback(async () => {
    if (!id) return;
    const requestId = loadRequestRef.current + 1;
    loadRequestRef.current = requestId;
    const currentWorkOrderId = parseInt(id, 10);

    try {
      setError('');
      const response = await api.getWorkOrder(currentWorkOrderId);
      if (requestId !== loadRequestRef.current) return;
      const hydratedWorkOrder = await hydrateOperationsFromShopFloor(response);
      if (requestId !== loadRequestRef.current) return;
      setWorkOrder(hydratedWorkOrder);
      
      // Load material requirements
      try {
        const matReqs = await api.getMaterialRequirements(currentWorkOrderId);
        if (requestId !== loadRequestRef.current) return;
        setMaterialReqs(matReqs);
      } catch {
        if (requestId !== loadRequestRef.current) return;
        // Material requirements may not exist for all parts
        setMaterialReqs(null);
      }
      try {
        const blockerRows = await api.getWorkOrderBlockers({ work_order_id: currentWorkOrderId, limit: 50 });
        if (requestId !== loadRequestRef.current) return;
        setBlockers(blockerRows);
      } catch {
        if (requestId !== loadRequestRef.current) return;
        setBlockers([]);
      }
    } catch {
      if (requestId !== loadRequestRef.current) return;
      setError('Failed to load work order');
    } finally {
      if (requestId !== loadRequestRef.current) return;
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
      if (!['work_order_update', 'shop_floor_update', 'dashboard_update'].includes(message.type)) return;
      const messageWorkOrderId = message.data?.work_order_id;
      if (workOrderId && messageWorkOrderId && messageWorkOrderId !== workOrderId) return;
      if (workOrderId && !messageWorkOrderId) return;
      scheduleRealtimeRefresh();
    }
  });

  useEffect(() => {
    setLoading(true);
    setError('');
    setWorkOrder(null);
    setMaterialReqs(null);
    setBlockers([]);
  }, [workOrderId]);

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
    const interval = window.setInterval(() => {
      loadWorkOrder();
    }, 30000);

    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') {
        loadWorkOrder();
      }
    };

    const refreshOnFocus = () => {
      loadWorkOrder();
    };

    document.addEventListener('visibilitychange', refreshWhenVisible);
    window.addEventListener('focus', refreshOnFocus);

    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
      window.removeEventListener('focus', refreshOnFocus);
    };
  }, [loadWorkOrder]);

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
      } catch {
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
      } catch {
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

  const handleDelete = async () => {
    if (!workOrder) return;
    const isCurrent = CURRENT_WORK_ORDER_STATUSES.includes(workOrder.status);
    const message = isCurrent
      ? `Delete current work order ${workOrder.work_order_number}?\n\nThis removes it from active lists, scheduling, and shop floor queues while preserving the record for audit/restore.`
      : `Delete work order ${workOrder.work_order_number}?\n\nThis removes it from active lists while preserving the record for audit/restore.`;
    if (!window.confirm(message)) return;

    setDeleting(true);
    try {
      await api.deleteWorkOrder(workOrder.id);
      navigate('/work-orders');
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to delete work order');
      setDeleting(false);
    }
  };

  /**
   * Parse a string from prompt() as a non-negative quantity. Returns null
   * (with a user-facing alert) if the input isn't a finite number or
   * exceeds `max`. Keeps garbage out of the completion API — the server
   * validates too, but surfacing the error client-side avoids a round-trip
   * for something that should obviously be rejected.
   */
  const parseQty = (raw: string | null, label: string, max?: number): number | null => {
    if (raw === null) return null; // user cancelled
    const trimmed = raw.trim();
    if (trimmed === '') return null;
    const n = Number(trimmed);
    if (!Number.isFinite(n) || n < 0) {
      alert(`${label} must be a non-negative number`);
      return null;
    }
    if (max !== undefined && n > max) {
      alert(`${label} cannot exceed ${max}`);
      return null;
    }
    return n;
  };

  const handleComplete = async () => {
    const ordered = workOrder!.quantity_ordered;
    const qtyCompleteRaw = prompt(`Enter quantity completed (ordered: ${ordered}):`, ordered.toString());
    const qtyComplete = parseQty(qtyCompleteRaw, 'Quantity completed', ordered);
    if (qtyComplete === null) return;

    const qtyScrappedRaw = prompt('Enter quantity scrapped (if any):', '0');
    const qtyScrapped = parseQty(qtyScrappedRaw ?? '0', 'Quantity scrapped');
    if (qtyScrapped === null) return;

    try {
      await api.completeWorkOrder(workOrder!.id, qtyComplete, qtyScrapped);
      loadWorkOrder();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to complete work order');
    }
  };

  const handleCompleteOperation = async (operation: WorkOrderOperation) => {
    const targetQty = Number(operation.component_quantity || workOrder!.quantity_ordered || 0);
    const qtyCompleteRaw = prompt(
      `Complete operation "${operation.name}"\nEnter quantity completed (target: ${targetQty}):`,
      targetQty.toString()
    );
    const qtyComplete = parseQty(qtyCompleteRaw, 'Quantity completed', targetQty);
    if (qtyComplete === null) return;

    const qtyScrappedRaw = prompt('Enter quantity scrapped (if any):', '0');
    const qtyScrapped = parseQty(qtyScrappedRaw ?? '0', 'Quantity scrapped');
    if (qtyScrapped === null) return;

    try {
      await api.completeWOOperation(operation.id, qtyComplete, qtyScrapped);
      loadWorkOrder();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to complete operation');
    }
  };

  const handleCreateBlocker = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!workOrder) return;

    setSubmittingBlocker(true);
    try {
      await api.createWorkOrderBlocker(workOrder.id, {
        operation_id: blockerForm.operation_id ? Number(blockerForm.operation_id) : undefined,
        category: blockerForm.category,
        severity: blockerForm.severity,
        note: blockerForm.note.trim() || undefined,
        put_operation_on_hold: true,
      });
      setBlockerForm({ operation_id: '', category: 'material_missing', severity: 'high', note: '' });
      await loadWorkOrder();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to report blocker');
    } finally {
      setSubmittingBlocker(false);
    }
  };

  const handleResolveBlocker = async (blocker: WorkOrderBlocker) => {
    const note = prompt(`Resolve blocker "${blocker.title}"?`, 'Resolved');
    if (note === null) return;
    setResolvingBlockerId(blocker.id);
    try {
      await api.resolveWorkOrderBlocker(blocker.id, note.trim() || undefined);
      await loadWorkOrder();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to resolve blocker');
    } finally {
      setResolvingBlockerId(null);
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

  const operationProgress = getDetailWorkOrderProgress(workOrder);

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
          {isAdminView && (
            <button
              onClick={handleDelete}
              disabled={deleting}
              className="btn-secondary flex items-center text-red-300 hover:text-red-200 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <TrashIcon className="h-5 w-5 mr-2" />
              {deleting ? 'Deleting...' : 'Delete'}
            </button>
          )}
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
              <dt className="text-sm text-slate-400">Operation Progress</dt>
              <dd className="text-lg font-medium text-werco-400">{operationProgress.label} ({operationProgress.percent}%)</dd>
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

      <div className="card">
        <div className="flex flex-col lg:flex-row lg:items-start lg:justify-between gap-4 mb-4">
          <div className="flex items-start gap-3">
            <ExclamationTriangleIcon className="h-6 w-6 text-amber-300 mt-0.5" />
            <div>
              <h2 className="text-lg font-semibold text-white">Blockers</h2>
              <p className="text-sm text-slate-400">
                Open issues that can stop this work order from moving cleanly.
              </p>
            </div>
          </div>
          <span className="text-xs font-semibold px-2 py-1 rounded bg-amber-500/20 text-amber-300 w-fit">
            {blockers.filter((item) => item.status === 'open' || item.status === 'acknowledged').length} open
          </span>
        </div>

        <div className="grid grid-cols-1 xl:grid-cols-[1fr_380px] gap-4">
          <div className="space-y-3">
            {blockers.length === 0 ? (
              <div className="rounded-lg border border-fd-line bg-slate-900/40 p-4 text-sm text-slate-400">
                No blockers reported.
              </div>
            ) : (
              blockers.map((blocker) => (
                <div key={blocker.id} className="rounded-lg border border-fd-line bg-slate-900/40 p-4">
                  <div className="flex flex-col md:flex-row md:items-start md:justify-between gap-3">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-semibold text-white">{blocker.title}</span>
                        <span className={`px-2 py-0.5 rounded text-xs font-semibold ${
                          blocker.severity === 'critical' || blocker.severity === 'high'
                            ? 'bg-red-500/20 text-red-300'
                            : blocker.severity === 'medium'
                              ? 'bg-amber-500/20 text-amber-300'
                              : 'bg-blue-500/20 text-blue-300'
                        }`}>
                          {blocker.severity}
                        </span>
                        <span className={`px-2 py-0.5 rounded text-xs font-semibold ${
                          blocker.status === 'resolved'
                            ? 'bg-emerald-500/20 text-emerald-300'
                            : 'bg-slate-700 text-slate-200'
                        }`}>
                          {blocker.status.replace('_', ' ')}
                        </span>
                      </div>
                      <div className="text-sm text-slate-400 mt-1">
                        {blocker.category.replace('_', ' ')}
                        {blocker.operation_name ? ` • ${blocker.operation_name}` : ''}
                        {blocker.material_part_number ? ` • ${blocker.material_part_number}` : ''}
                      </div>
                      {blocker.note && <p className="text-sm text-slate-300 mt-2">{blocker.note}</p>}
                    </div>
                    {(blocker.status === 'open' || blocker.status === 'acknowledged') && (
                      <button
                        onClick={() => handleResolveBlocker(blocker)}
                        disabled={resolvingBlockerId === blocker.id}
                        className="btn-success btn-sm"
                      >
                        {resolvingBlockerId === blocker.id ? 'Resolving...' : 'Resolve'}
                      </button>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>

          <form onSubmit={handleCreateBlocker} className="rounded-lg border border-fd-line bg-slate-900/40 p-4 space-y-3">
            <h3 className="font-semibold text-white">Report Blocker</h3>
            <div>
              <label className="text-sm text-slate-400 block mb-1">Operation</label>
              <select
                value={blockerForm.operation_id}
                onChange={(e) => setBlockerForm({ ...blockerForm, operation_id: e.target.value })}
                className="input"
              >
                <option value="">Whole work order</option>
                {workOrder.operations.map((op) => (
                  <option key={op.id} value={op.id}>
                    {op.operation_number || `Op ${op.sequence}`} - {op.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-sm text-slate-400 block mb-1">Category</label>
                <select
                  value={blockerForm.category}
                  onChange={(e) => setBlockerForm({ ...blockerForm, category: e.target.value as WorkOrderBlockerCategory })}
                  className="input"
                >
                  <option value="material_missing">Material missing</option>
                  <option value="machine_down">Machine down</option>
                  <option value="tooling_missing">Tooling missing</option>
                  <option value="quality_hold">Quality hold</option>
                  <option value="labor_unavailable">Labor unavailable</option>
                  <option value="engineering_question">Engineering question</option>
                  <option value="other">Other</option>
                </select>
              </div>
              <div>
                <label className="text-sm text-slate-400 block mb-1">Severity</label>
                <select
                  value={blockerForm.severity}
                  onChange={(e) => setBlockerForm({ ...blockerForm, severity: e.target.value as WorkOrderBlockerSeverity })}
                  className="input"
                >
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                  <option value="critical">Critical</option>
                  <option value="low">Low</option>
                </select>
              </div>
            </div>
            <div>
              <label className="text-sm text-slate-400 block mb-1">Note</label>
              <textarea
                value={blockerForm.note}
                onChange={(e) => setBlockerForm({ ...blockerForm, note: e.target.value })}
                className="input"
                rows={3}
                maxLength={2000}
                placeholder="What is stopping the job?"
              />
            </div>
            <button type="submit" disabled={submittingBlocker} className="btn-primary w-full">
              {submittingBlocker ? 'Reporting...' : 'Report Blocker'}
            </button>
          </form>
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
                <tbody className="bg-fd-panel divide-y divide-slate-700">
                  {activeUsersOnWorkOrder.map((entry) => (
                    <tr key={`${entry.user_id}-${entry.clock_in ?? ''}-${entry.operation ?? 'op'}`} className="hover:bg-slate-800/50">
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
              <tbody className="bg-fd-panel divide-y divide-slate-700">
                {(() => {
                  let lastGroup = '';
                  return workOrder.operations.map((op) => {
                    const isNewGroup = op.operation_group && op.operation_group !== lastGroup;
                    if (op.operation_group) lastGroup = op.operation_group;
                    const operationTarget = Number(op.component_quantity || workOrder.quantity_ordered || 0);
                    
                    const groupColors: Record<string, string> = {
                      'LASER': 'bg-fd-red/15 text-fd-red',
                      'MACHINE': 'bg-fd-blue/15 text-fd-blue',
                      'BEND': 'bg-fd-amber/15 text-fd-amber',
                      'WELD': 'bg-amber-500/15 text-amber-300',
                      'FINISH': 'bg-fd-cyan/15 text-fd-cyan',
                      'ASSEMBLY': 'bg-fd-green/15 text-fd-green',
                      'INSPECT': 'bg-fd-blue/15 text-fd-blue',
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
                          <div>
                            <span className="font-medium text-sm">{op.quantity_complete}</span>
                            <span className="text-slate-400 text-sm">/{operationTarget}</span>
                          </div>
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
                              onClick={() => handleCompleteOperation(op)}
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
              <tbody className="bg-fd-panel divide-y divide-slate-700">
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
