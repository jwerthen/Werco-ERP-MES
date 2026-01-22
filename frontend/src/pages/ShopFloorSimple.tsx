import React, { useEffect, useState, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import { format } from 'date-fns';
import {
  PlayIcon,
  CheckCircleIcon,
  XMarkIcon,
  WrenchScrewdriverIcon,
  ArrowPathIcon,
  MagnifyingGlassIcon,
  EyeIcon,
  PauseIcon,
  ExclamationTriangleIcon,
  ClockIcon,
  CubeIcon,
} from '@heroicons/react/24/solid';
import { FunnelIcon } from '@heroicons/react/24/outline';

interface Operation {
  id: number;
  work_order_id: number;
  work_order_number: string;
  part_number: string | null;
  part_name: string | null;
  operation_number: string;
  operation_name: string;
  description: string | null;
  work_center_id: number | null;
  work_center_name: string | null;
  status: string;
  quantity_ordered: number;
  quantity_complete: number;
  quantity_scrapped: number;
  priority: number;
  due_date: string | null;
  customer_name: string | null;
  customer_po: string | null;
  actual_start: string | null;
  setup_instructions: string | null;
  run_instructions: string | null;
  requires_inspection: boolean;
}

interface WorkCenter {
  id: number;
  name: string;
  code: string;
}

interface Toast {
  id: number;
  type: 'success' | 'error' | 'info';
  message: string;
}

const STATUS_COLORS: Record<string, { bg: string; text: string; dot: string }> = {
  pending: { bg: 'bg-gray-100', text: 'text-gray-700', dot: 'bg-gray-400' },
  ready: { bg: 'bg-blue-100', text: 'text-blue-700', dot: 'bg-blue-500' },
  in_progress: { bg: 'bg-amber-100', text: 'text-amber-700', dot: 'bg-amber-500' },
  complete: { bg: 'bg-green-100', text: 'text-green-700', dot: 'bg-green-500' },
  on_hold: { bg: 'bg-red-100', text: 'text-red-700', dot: 'bg-red-500' },
};

export default function ShopFloorSimple() {
  const navigate = useNavigate();
  const [operations, setOperations] = useState<Operation[]>([]);
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  
  // Filters
  const [workCenterId, setWorkCenterId] = useState<number | ''>('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [dueTodayOnly, setDueTodayOnly] = useState(false);
  
  // Modal states
  const [completeModal, setCompleteModal] = useState<Operation | null>(null);
  const [detailsModal, setDetailsModal] = useState<any | null>(null);
  const [completeQty, setCompleteQty] = useState(0);
  const [completeNotes, setCompleteNotes] = useState('');
  const [actionLoading, setActionLoading] = useState<number | null>(null);
  
  // Toast notifications
  const [toasts, setToasts] = useState<Toast[]>([]);

  // Debounce search
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  // Load data
  const loadOperations = useCallback(async () => {
    try {
      const params: any = {};
      if (workCenterId) params.work_center_id = workCenterId;
      if (statusFilter) params.status = statusFilter;
      if (dueTodayOnly) params.due_today = true;
      if (debouncedSearch) params.search = debouncedSearch;
      
      const response = await api.getShopFloorOperations(params);
      setOperations(response.operations || []);
    } catch (err) {
      console.error('Failed to load operations:', err);
      showToast('error', 'Failed to load operations');
    }
  }, [workCenterId, statusFilter, debouncedSearch, dueTodayOnly]);

  const loadWorkCenters = async () => {
    try {
      const response = await api.getWorkCenters();
      setWorkCenters(response || []);
    } catch (err) {
      console.error('Failed to load work centers:', err);
    }
  };

  useEffect(() => {
    const init = async () => {
      setLoading(true);
      await Promise.all([loadOperations(), loadWorkCenters()]);
      setLoading(false);
    };
    init();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-refresh every 30 seconds
  useEffect(() => {
    const interval = setInterval(() => {
      loadOperations();
    }, 30000);
    return () => clearInterval(interval);
  }, [loadOperations]);

  const handleRefresh = async () => {
    setRefreshing(true);
    await loadOperations();
    setRefreshing(false);
  };

  const showToast = (type: 'success' | 'error' | 'info', message: string) => {
    const id = Date.now();
    setToasts(prev => [...prev, { id, type, message }]);
    setTimeout(() => {
      setToasts(prev => prev.filter(t => t.id !== id));
    }, 4000);
  };

  const isOverdue = (dueDate: string | null) => {
    if (!dueDate) return false;
    return new Date(dueDate) < new Date();
  };

  const isDueToday = (dueDate: string | null) => {
    if (!dueDate) return false;
    const due = new Date(dueDate);
    const today = new Date();
    return (
      due.getFullYear() === today.getFullYear() &&
      due.getMonth() === today.getMonth() &&
      due.getDate() === today.getDate()
    );
  };

  const workCenterBuckets = useMemo(() => {
    const buckets = new Map<number, {
      id: number;
      name: string;
      total: number;
      open: number;
      inProgress: number;
      onHold: number;
      dueToday: number;
      overdue: number;
    }>();

    workCenters.forEach((wc) => {
      buckets.set(wc.id, {
        id: wc.id,
        name: wc.name,
        total: 0,
        open: 0,
        inProgress: 0,
        onHold: 0,
        dueToday: 0,
        overdue: 0,
      });
    });

    operations.forEach((op) => {
      if (!op.work_center_id) return;
      const bucket = buckets.get(op.work_center_id) || {
        id: op.work_center_id,
        name: op.work_center_name || `Work Center ${op.work_center_id}`,
        total: 0,
        open: 0,
        inProgress: 0,
        onHold: 0,
        dueToday: 0,
        overdue: 0,
      };

      bucket.total += 1;
      if (op.status === 'pending' || op.status === 'ready') bucket.open += 1;
      if (op.status === 'in_progress') bucket.inProgress += 1;
      if (op.status === 'on_hold') bucket.onHold += 1;
      if (isDueToday(op.due_date)) bucket.dueToday += 1;
      if (isOverdue(op.due_date)) bucket.overdue += 1;

      buckets.set(bucket.id, bucket);
    });

    return Array.from(buckets.values()).sort((a, b) => a.name.localeCompare(b.name));
  }, [operations, workCenters]);

  // Action handlers
  const handleStart = async (operation: Operation) => {
    setActionLoading(operation.id);
    try {
      await api.startOperation(operation.id);
      showToast('success', `Started ${operation.operation_number} - ${operation.operation_name}`);
      await loadOperations();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to start operation');
    } finally {
      setActionLoading(null);
    }
  };

  const handleOpenComplete = (operation: Operation) => {
    setCompleteModal(operation);
    setCompleteQty(operation.quantity_complete);
    setCompleteNotes('');
  };

  const handleComplete = async () => {
    if (!completeModal) return;
    
    setActionLoading(completeModal.id);
    try {
      const result = await api.completeOperation(completeModal.id, {
        quantity_complete: completeQty,
        notes: completeNotes || undefined,
      });
      
      if (result.is_fully_complete) {
        showToast('success', `Completed ${completeModal.operation_number} - All ${completeQty} units done!`);
      } else {
        showToast('info', `Progress updated: ${completeQty} / ${completeModal.quantity_ordered} complete`);
      }
      
      setCompleteModal(null);
      await loadOperations();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to update progress');
    } finally {
      setActionLoading(null);
    }
  };

  const handleViewDetails = async (operation: Operation) => {
    try {
      const details = await api.getOperationDetails(operation.id);
      setDetailsModal(details);
    } catch (err: any) {
      showToast('error', 'Failed to load operation details');
    }
  };

  const handleHold = async (operationId: number) => {
    setActionLoading(operationId);
    try {
      await api.holdOperation(operationId);
      showToast('info', 'Operation placed on hold');
      await loadOperations();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to hold operation');
    } finally {
      setActionLoading(null);
    }
  };

  const handleResume = async (operationId: number) => {
    setActionLoading(operationId);
    try {
      await api.resumeOperation(operationId);
      showToast('success', 'Operation resumed');
      await loadOperations();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to resume operation');
    } finally {
      setActionLoading(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <ArrowPathIcon className="h-12 w-12 animate-spin text-werco-primary mx-auto mb-4" />
          <p className="text-surface-500">Loading shop floor...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Toast Notifications */}
      <div className="fixed top-4 right-4 z-50 space-y-2">
        {toasts.map(toast => (
          <div
            key={toast.id}
            className={`px-4 py-3 rounded-lg shadow-lg flex items-center gap-3 animate-slide-in ${
              toast.type === 'success' ? 'bg-green-600 text-white' :
              toast.type === 'error' ? 'bg-red-600 text-white' :
              'bg-blue-600 text-white'
            }`}
          >
            {toast.type === 'success' && <CheckCircleIcon className="h-5 w-5" />}
            {toast.type === 'error' && <ExclamationTriangleIcon className="h-5 w-5" />}
            {toast.type === 'info' && <ClockIcon className="h-5 w-5" />}
            <span className="font-medium">{toast.message}</span>
          </div>
        ))}
      </div>

      {/* Page Header */}
      <div className="page-header">
        <div>
          <h1 className="page-title flex items-center gap-3">
            <WrenchScrewdriverIcon className="h-8 w-8 text-werco-600" />
            Shop Floor Operations
          </h1>
          <p className="page-subtitle">Start and complete work order operations</p>
        </div>
        <div className="page-actions">
          <button 
            onClick={handleRefresh}
            disabled={refreshing}
            className="btn-secondary"
          >
            <ArrowPathIcon className={`h-5 w-5 mr-2 ${refreshing ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="card" data-tour="sf-clock">
        <div className="flex flex-wrap gap-4 items-center">
          <div className="flex-1 min-w-[200px]">
            <div className="relative">
              <MagnifyingGlassIcon className="absolute left-3 top-1/2 -translate-y-1/2 h-5 w-5 text-gray-400" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search WO# or Part#..."
                className="input pl-10"
              />
            </div>
          </div>
          
          <div className="flex items-center gap-2">
            <FunnelIcon className="h-5 w-5 text-gray-400" />
            <select
              value={workCenterId}
              onChange={(e) => setWorkCenterId(e.target.value ? Number(e.target.value) : '')}
              className="input w-48"
            >
              <option value="">All Work Centers</option>
              {workCenters.map(wc => (
                <option key={wc.id} value={wc.id}>{wc.name}</option>
              ))}
            </select>
            
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="input w-40"
            >
              <option value="">All Status</option>
              <option value="pending">Pending</option>
              <option value="ready">Ready</option>
              <option value="in_progress">In Progress</option>
              <option value="on_hold">On Hold</option>
            </select>
          </div>
          
          <div className="text-sm text-gray-500">
            {operations.length} operation{operations.length !== 1 ? 's' : ''}
          </div>
        </div>
      </div>

      {/* Work Cell Buckets */}
      <div className="space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Work Cell Buckets</h2>
            <p className="text-sm text-gray-500">Click a cell to focus the queue</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => setStatusFilter('')}
              className={`rounded-full border px-3 py-1 text-xs font-medium transition ${
                statusFilter === '' ? 'border-werco-500 bg-werco-50 text-werco-700' : 'border-gray-200 text-gray-600 hover:border-werco-300'
              }`}
            >
              All
            </button>
            <button
              type="button"
              onClick={() => setStatusFilter('pending')}
              className={`rounded-full border px-3 py-1 text-xs font-medium transition ${
                statusFilter === 'pending' ? 'border-gray-500 bg-gray-100 text-gray-700' : 'border-gray-200 text-gray-600 hover:border-gray-400'
              }`}
            >
              Open
            </button>
            <button
              type="button"
              onClick={() => setStatusFilter('in_progress')}
              className={`rounded-full border px-3 py-1 text-xs font-medium transition ${
                statusFilter === 'in_progress' ? 'border-amber-500 bg-amber-100 text-amber-700' : 'border-gray-200 text-gray-600 hover:border-amber-300'
              }`}
            >
              In Progress
            </button>
            <button
              type="button"
              onClick={() => setStatusFilter('on_hold')}
              className={`rounded-full border px-3 py-1 text-xs font-medium transition ${
                statusFilter === 'on_hold' ? 'border-red-500 bg-red-100 text-red-700' : 'border-gray-200 text-gray-600 hover:border-red-300'
              }`}
            >
              On Hold
            </button>
            <button
              type="button"
              onClick={() => setStatusFilter('ready')}
              className={`rounded-full border px-3 py-1 text-xs font-medium transition ${
                statusFilter === 'ready' ? 'border-blue-500 bg-blue-100 text-blue-700' : 'border-gray-200 text-gray-600 hover:border-blue-300'
              }`}
            >
              Ready
            </button>
            <button
              type="button"
              onClick={() => setDueTodayOnly((prev) => !prev)}
              className={`rounded-full border px-3 py-1 text-xs font-medium transition ${
                dueTodayOnly ? 'border-blue-500 bg-blue-100 text-blue-700' : 'border-gray-200 text-gray-600 hover:border-blue-300'
              }`}
            >
              Due Today
            </button>
          </div>
        </div>
        <div className="flex gap-3 overflow-x-auto pb-1">
          <button
            type="button"
            onClick={() => setWorkCenterId('')}
            className={`min-w-[220px] text-left rounded-xl border px-4 py-3 transition ${
              workCenterId === '' ? 'border-werco-500 bg-werco-50 shadow-sm' : 'border-gray-200 bg-white hover:border-werco-300'
            }`}
          >
            <div className="text-sm text-gray-500">All Cells</div>
            <div className="text-lg font-semibold text-gray-900">{operations.length} ops</div>
            <div className="mt-2 flex flex-wrap gap-2 text-xs">
              <span className="rounded-full bg-gray-100 px-2 py-0.5 text-gray-600">
                Open {operations.filter(op => op.status === 'pending' || op.status === 'ready').length}
              </span>
              <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-700">
                In Progress {operations.filter(op => op.status === 'in_progress').length}
              </span>
              <span className="rounded-full bg-blue-100 px-2 py-0.5 text-blue-700">
                Due Today {operations.filter(op => isDueToday(op.due_date)).length}
              </span>
            </div>
          </button>
          {workCenterBuckets.map((bucket) => (
            <button
              key={bucket.id}
              type="button"
              onClick={() => setWorkCenterId(bucket.id)}
              className={`min-w-[220px] text-left rounded-xl border px-4 py-3 transition ${
                workCenterId === bucket.id ? 'border-werco-500 bg-werco-50 shadow-sm' : 'border-gray-200 bg-white hover:border-werco-300'
              }`}
            >
              <div className="text-sm text-gray-500">{bucket.name}</div>
              <div className="text-lg font-semibold text-gray-900">{bucket.total} ops</div>
              <div className="mt-2 flex flex-wrap gap-2 text-xs">
                <span className="rounded-full bg-gray-100 px-2 py-0.5 text-gray-600">
                  Open {bucket.open}
                </span>
                <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-700">
                  In Progress {bucket.inProgress}
                </span>
                <span className="rounded-full bg-blue-100 px-2 py-0.5 text-blue-700">
                  Due Today {bucket.dueToday}
                </span>
                {bucket.overdue > 0 && (
                  <span className="rounded-full bg-red-100 px-2 py-0.5 text-red-700">
                    Overdue {bucket.overdue}
                  </span>
                )}
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Operations Grid */}
      {operations.length === 0 ? (
        <div className="card text-center py-16">
          <CubeIcon className="h-12 w-12 text-gray-300 mx-auto mb-4" />
          <p className="text-gray-600 font-medium">No operations found</p>
          <p className="text-sm text-gray-500 mt-1">Try adjusting your filters</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4" data-tour="sf-operations">
          {operations.map(op => {
            const colors = STATUS_COLORS[op.status] || STATUS_COLORS.pending;
            const progress = op.quantity_ordered > 0 
              ? (op.quantity_complete / op.quantity_ordered) * 100 
              : 0;
            const overdue = isOverdue(op.due_date);
            
            return (
              <div 
                key={op.id} 
                className={`card hover:shadow-lg transition-shadow ${overdue ? 'border-red-300 bg-red-50/30' : ''}`}
              >
                {/* Header */}
                <div className="flex items-start justify-between mb-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-werco-primary text-lg">{op.work_order_number}</span>
                      {overdue && (
                        <span className="px-2 py-0.5 bg-red-100 text-red-700 text-xs font-semibold rounded">
                          OVERDUE
                        </span>
                      )}
                    </div>
                    <p className="text-sm text-gray-600">{op.part_number}</p>
                  </div>
                  <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold ${colors.bg} ${colors.text}`}>
                    <span className={`w-1.5 h-1.5 rounded-full ${colors.dot}`}></span>
                    {op.status.replace('_', ' ')}
                  </span>
                </div>
                
                {/* Operation Info */}
                <div className="bg-gray-50 rounded-lg p-3 mb-3">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="font-semibold text-gray-900">
                      {op.operation_number} - {op.operation_name}
                    </span>
                  </div>
                  <p className="text-sm text-gray-600">{op.part_name}</p>
                  {op.work_center_name && (
                    <p className="text-xs text-gray-500 mt-1">
                      Work Center: {op.work_center_name}
                    </p>
                  )}
                </div>
                
                {/* Progress */}
                <div className="mb-3">
                  <div className="flex justify-between text-sm mb-1">
                    <span className="font-medium text-gray-700">Progress</span>
                    <span className="font-semibold tabular-nums">
                      {op.quantity_complete} / {op.quantity_ordered}
                    </span>
                  </div>
                  <div className="h-2.5 bg-gray-200 rounded-full overflow-hidden">
                    <div 
                      className={`h-full rounded-full transition-all ${progress >= 100 ? 'bg-green-500' : 'bg-werco-600'}`}
                      style={{ width: `${Math.min(100, progress)}%` }}
                    />
                  </div>
                  <div className="text-right text-xs text-gray-500 mt-0.5">
                    {Math.round(progress)}% complete
                  </div>
                </div>
                
                {/* Meta Info */}
                <div className="flex items-center justify-between text-sm text-gray-600 mb-4">
                  <div>
                    {op.due_date && (
                      <span className={overdue ? 'text-red-600 font-medium' : ''}>
                        Due: {format(new Date(op.due_date), 'MMM d')}
                      </span>
                    )}
                  </div>
                  <div>
                    {op.priority && (
                      <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                        op.priority <= 2 ? 'bg-red-100 text-red-700' :
                        op.priority <= 5 ? 'bg-amber-100 text-amber-700' :
                        'bg-gray-100 text-gray-600'
                      }`}>
                        P{op.priority}
                      </span>
                    )}
                  </div>
                </div>
                
                {/* Action Buttons */}
                <div className="flex gap-2" data-tour="sf-complete">
                  {/* Start Button - visible when pending or ready */}
                  {(op.status === 'pending' || op.status === 'ready') && (
                    <button
                      onClick={() => handleStart(op)}
                      disabled={actionLoading === op.id}
                      className="flex-1 btn-primary text-sm py-2.5"
                    >
                      {actionLoading === op.id ? (
                        <ArrowPathIcon className="h-4 w-4 animate-spin mx-auto" />
                      ) : (
                        <>
                          <PlayIcon className="h-4 w-4 mr-1.5" />
                          Start Operation
                        </>
                      )}
                    </button>
                  )}
                  
                  {/* Complete Button - visible when in progress */}
                  {op.status === 'in_progress' && (
                    <button
                      onClick={() => handleOpenComplete(op)}
                      disabled={actionLoading === op.id}
                      className="flex-1 btn-success text-sm py-2.5"
                    >
                      {actionLoading === op.id ? (
                        <ArrowPathIcon className="h-4 w-4 animate-spin mx-auto" />
                      ) : (
                        <>
                          <CheckCircleIcon className="h-4 w-4 mr-1.5" />
                          Mark Complete
                        </>
                      )}
                    </button>
                  )}
                  
                  {/* Hold Button - visible when in progress */}
                  {op.status === 'in_progress' && (
                    <button
                      onClick={() => handleHold(op.id)}
                      disabled={actionLoading === op.id}
                      className="btn-secondary text-sm py-2.5 px-3"
                      title="Put on Hold"
                    >
                      <PauseIcon className="h-4 w-4" />
                    </button>
                  )}
                  
                  {/* Resume Button - visible when on hold */}
                  {op.status === 'on_hold' && (
                    <button
                      onClick={() => handleResume(op.id)}
                      disabled={actionLoading === op.id}
                      className="flex-1 btn-primary text-sm py-2.5"
                    >
                      {actionLoading === op.id ? (
                        <ArrowPathIcon className="h-4 w-4 animate-spin mx-auto" />
                      ) : (
                        <>
                          <PlayIcon className="h-4 w-4 mr-1.5" />
                          Resume
                        </>
                      )}
                    </button>
                  )}
                  
                  {/* View Details Button - always visible */}
                  <button
                    onClick={() => handleViewDetails(op)}
                    className="btn-secondary text-sm py-2.5 px-3"
                    title="View Details"
                  >
                    <EyeIcon className="h-4 w-4" />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Complete Operation Modal */}
      {completeModal && (
        <div className="modal-overlay" onClick={() => setCompleteModal(null)}>
          <div className="modal max-w-md" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">Mark Complete</h3>
              <button onClick={() => setCompleteModal(null)} className="p-2 rounded-lg hover:bg-gray-100">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            
            <div className="modal-body space-y-4">
              <div className="bg-gray-50 rounded-lg p-4">
                <p className="text-sm text-gray-500">Operation</p>
                <p className="font-semibold text-gray-900">
                  {completeModal.operation_number} - {completeModal.operation_name}
                </p>
                <p className="text-sm text-gray-600 mt-1">
                  {completeModal.work_order_number} • {completeModal.part_number}
                </p>
              </div>
              
              <div>
                <label className="label">Quantity Complete</label>
                <div className="flex items-center gap-3">
                  <input
                    type="number"
                    min={0}
                    max={completeModal.quantity_ordered}
                    value={completeQty}
                    onChange={(e) => setCompleteQty(Number(e.target.value))}
                    className="input text-center text-2xl font-bold flex-1"
                  />
                  <span className="text-gray-500">/ {completeModal.quantity_ordered}</span>
                </div>
                <p className="text-sm text-gray-500 mt-1">
                  {completeQty >= completeModal.quantity_ordered 
                    ? '✓ This will mark the operation as COMPLETE'
                    : 'Partial completion - operation will remain IN PROGRESS'}
                </p>
              </div>
              
              <div>
                <label className="label">Notes (optional)</label>
                <textarea
                  value={completeNotes}
                  onChange={(e) => setCompleteNotes(e.target.value)}
                  className="input"
                  rows={3}
                  placeholder="Any issues, observations, or notes..."
                />
              </div>
            </div>
            
            <div className="modal-footer">
              <button onClick={() => setCompleteModal(null)} className="btn-secondary">
                Cancel
              </button>
              <button
                onClick={handleComplete}
                disabled={actionLoading === completeModal.id || completeQty < 0}
                className="btn-success"
              >
                {actionLoading === completeModal.id ? (
                  <ArrowPathIcon className="h-5 w-5 animate-spin" />
                ) : (
                  <>
                    <CheckCircleIcon className="h-5 w-5 mr-2" />
                    Confirm Complete
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Operation Details Modal */}
      {detailsModal && (
        <div className="modal-overlay" onClick={() => setDetailsModal(null)}>
          <div className="modal max-w-2xl" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3 className="text-lg font-semibold">Operation Details</h3>
              <button onClick={() => setDetailsModal(null)} className="p-2 rounded-lg hover:bg-gray-100">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            
            <div className="modal-body space-y-6 max-h-[70vh] overflow-y-auto">
              {/* Work Order Info */}
              <div className="bg-werco-50 rounded-lg p-4">
                <div className="flex justify-between items-start">
                  <div>
                    <p className="text-sm text-werco-600 font-medium">Work Order</p>
                    <p className="text-xl font-bold text-gray-900">{detailsModal.work_order.work_order_number}</p>
                    <p className="text-gray-600">{detailsModal.work_order.part?.part_number} - {detailsModal.work_order.part?.name}</p>
                  </div>
                  <button
                    onClick={() => {
                      setDetailsModal(null);
                      navigate(`/work-orders/${detailsModal.work_order.id}`);
                    }}
                    className="btn-secondary btn-sm"
                  >
                    Open Full WO
                  </button>
                </div>
              </div>
              
              {/* Operation Info */}
              <div>
                <h4 className="font-semibold text-gray-900 mb-3">Current Operation</h4>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <p className="text-sm text-gray-500">Operation</p>
                    <p className="font-medium">{detailsModal.operation.operation_number} - {detailsModal.operation.name}</p>
                  </div>
                  <div>
                    <p className="text-sm text-gray-500">Work Center</p>
                    <p className="font-medium">{detailsModal.work_center?.name || '—'}</p>
                  </div>
                  <div>
                    <p className="text-sm text-gray-500">Quantity</p>
                    <p className="font-medium">{detailsModal.operation.quantity_complete} / {detailsModal.work_order.quantity_ordered}</p>
                  </div>
                  <div>
                    <p className="text-sm text-gray-500">Status</p>
                    <p className="font-medium capitalize">{detailsModal.operation.status.replace('_', ' ')}</p>
                  </div>
                </div>
              </div>
              
              {/* Instructions */}
              {(detailsModal.operation.setup_instructions || detailsModal.operation.run_instructions) && (
                <div>
                  <h4 className="font-semibold text-gray-900 mb-3">Work Instructions</h4>
                  {detailsModal.operation.setup_instructions && (
                    <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 mb-2">
                      <p className="text-sm font-medium text-amber-800">Setup Instructions</p>
                      <p className="text-sm text-amber-700 whitespace-pre-wrap">{detailsModal.operation.setup_instructions}</p>
                    </div>
                  )}
                  {detailsModal.operation.run_instructions && (
                    <div className="bg-blue-50 border border-blue-200 rounded-lg p-3">
                      <p className="text-sm font-medium text-blue-800">Run Instructions</p>
                      <p className="text-sm text-blue-700 whitespace-pre-wrap">{detailsModal.operation.run_instructions}</p>
                    </div>
                  )}
                </div>
              )}
              
              {/* All Operations */}
              <div>
                <h4 className="font-semibold text-gray-900 mb-3">All Operations</h4>
                <div className="space-y-2">
                  {detailsModal.all_operations.map((op: any) => {
                    const opColors = STATUS_COLORS[op.status] || STATUS_COLORS.pending;
                    return (
                      <div 
                        key={op.id} 
                        className={`flex items-center justify-between p-3 rounded-lg ${op.is_current ? 'bg-werco-100 border border-werco-300' : 'bg-gray-50'}`}
                      >
                        <div className="flex items-center gap-3">
                          <span className="text-sm font-medium text-gray-500 w-12">{op.operation_number}</span>
                          <span className="font-medium">{op.name}</span>
                          {op.is_current && <span className="text-xs bg-werco-600 text-white px-2 py-0.5 rounded">Current</span>}
                        </div>
                        <span className={`px-2 py-0.5 rounded text-xs font-medium ${opColors.bg} ${opColors.text}`}>
                          {op.status.replace('_', ' ')}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
              
              {/* History */}
              {detailsModal.history && detailsModal.history.length > 0 && (
                <div>
                  <h4 className="font-semibold text-gray-900 mb-3">Recent History</h4>
                  <div className="space-y-2">
                    {detailsModal.history.map((h: any, i: number) => (
                      <div key={i} className="flex items-start gap-3 text-sm">
                        <span className="text-gray-400 w-32 flex-shrink-0">
                          {h.created_at ? format(new Date(h.created_at), 'MMM d, h:mm a') : '—'}
                        </span>
                        <span className="text-gray-700">{h.details}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
            
            <div className="modal-footer">
              <button onClick={() => setDetailsModal(null)} className="btn-secondary">
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      <style>{`
        @keyframes slide-in {
          from {
            transform: translateX(100%);
            opacity: 0;
          }
          to {
            transform: translateX(0);
            opacity: 1;
          }
        }
        .animate-slide-in {
          animation: slide-in 0.3s ease-out;
        }
      `}</style>
    </div>
  );
}
