import React, { useEffect, useState, useMemo, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import api from '../services/api';
import { WorkOrderSummary, WorkOrderStatus } from '../types';
import { format } from 'date-fns';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { 
  PlusIcon, 
  MagnifyingGlassIcon, 
  Squares2X2Icon, 
  ListBulletIcon,
  ChevronRightIcon,
  ClockIcon,
  ExclamationTriangleIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';
import { SkeletonTable, SkeletonCard } from '../components/ui/Skeleton';

const statusConfig: Record<WorkOrderStatus, { bg: string; text: string; dot: string }> = {
  draft: { bg: 'bg-surface-100', text: 'text-surface-700', dot: 'bg-surface-400' },
  released: { bg: 'bg-blue-50', text: 'text-blue-700', dot: 'bg-blue-500' },
  in_progress: { bg: 'bg-emerald-50', text: 'text-emerald-700', dot: 'bg-emerald-500' },
  on_hold: { bg: 'bg-amber-50', text: 'text-amber-700', dot: 'bg-amber-500' },
  complete: { bg: 'bg-emerald-50', text: 'text-emerald-700', dot: 'bg-emerald-500' },
  closed: { bg: 'bg-surface-100', text: 'text-surface-500', dot: 'bg-surface-400' },
  cancelled: { bg: 'bg-red-50', text: 'text-red-700', dot: 'bg-red-500' },
};

const priorityConfig: Record<number, { bg: string; text: string; label: string }> = {
  1: { bg: 'bg-red-100', text: 'text-red-700', label: 'Critical' },
  2: { bg: 'bg-red-50', text: 'text-red-600', label: 'High' },
  3: { bg: 'bg-amber-50', text: 'text-amber-700', label: 'Medium' },
  4: { bg: 'bg-blue-50', text: 'text-blue-600', label: 'Normal' },
  5: { bg: 'bg-surface-100', text: 'text-surface-600', label: 'Low' },
};

const EXCLUDED_PART_TYPES = ['purchased', 'hardware', 'raw_material'];

type GroupBy = 'none' | 'customer' | 'part' | 'status';

export default function WorkOrders() {
  const [workOrders, setWorkOrders] = useState<WorkOrderSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [customerFilter, setCustomerFilter] = useState<string>('');
  const [hideCOTS, setHideCOTS] = useState(true);
  const [groupBy, setGroupBy] = useState<GroupBy>('none');
  const realtimeRefreshRef = useRef<NodeJS.Timeout | null>(null);
  const realtimeUrl = useMemo(() => {
    const token = getAccessToken();
    return buildWsUrl('/ws/updates', token ? { token } : undefined);
  }, []);

  const loadWorkOrders = useCallback(async () => {
    try {
      const params: any = {};
      if (statusFilter) params.status = statusFilter;
      const response = await api.getWorkOrders(params);
      setWorkOrders(response);
    } catch (err) {
      console.error('Failed to load work orders:', err);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  const scheduleRealtimeRefresh = useCallback(() => {
    if (realtimeRefreshRef.current) return;
    realtimeRefreshRef.current = setTimeout(() => {
      realtimeRefreshRef.current = null;
      loadWorkOrders();
    }, 600);
  }, [loadWorkOrders]);

  useWebSocket({
    url: realtimeUrl,
    enabled: true,
    onMessage: (message) => {
      if (message.type === 'connected' || message.type === 'ping') return;
      if (['work_order_update', 'dashboard_update', 'shop_floor_update'].includes(message.type)) {
        scheduleRealtimeRefresh();
      }
    }
  });

  useEffect(() => {
    loadWorkOrders();
  }, [loadWorkOrders]);

  useEffect(() => {
    return () => {
      if (realtimeRefreshRef.current) {
        clearTimeout(realtimeRefreshRef.current);
        realtimeRefreshRef.current = null;
      }
    };
  }, []);

  const handleDelete = async (wo: WorkOrderSummary) => {
    const canDelete = wo.status === 'draft' || wo.status === 'cancelled';
    if (!canDelete) {
      alert('Only draft or cancelled work orders can be deleted.');
      return;
    }
    if (!window.confirm(`Delete work order ${wo.work_order_number}?`)) return;
    try {
      await api.deleteWorkOrder(wo.id);
      loadWorkOrders();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to delete work order');
    }
  };

  const customers = useMemo(() => {
    const unique = new Set(workOrders.map(wo => wo.customer_name).filter(Boolean));
    return Array.from(unique).sort() as string[];
  }, [workOrders]);

  const filteredWorkOrders = useMemo(() => {
    return workOrders.filter(wo => {
      if (hideCOTS && wo.part_type && EXCLUDED_PART_TYPES.includes(wo.part_type)) {
        return false;
      }
      if (customerFilter && wo.customer_name !== customerFilter) {
        return false;
      }
      if (search) {
        const searchLower = search.toLowerCase();
        return (
          wo.work_order_number.toLowerCase().includes(searchLower) ||
          wo.part_number?.toLowerCase().includes(searchLower) ||
          wo.part_name?.toLowerCase().includes(searchLower) ||
          wo.customer_name?.toLowerCase().includes(searchLower)
        );
      }
      return true;
    });
  }, [workOrders, search, customerFilter, hideCOTS]);

  const groupedWorkOrders = useMemo(() => {
    if (groupBy === 'none') return null;
    
    const groups: Record<string, WorkOrderSummary[]> = {};
    filteredWorkOrders.forEach(wo => {
      let key: string;
      switch (groupBy) {
        case 'customer':
          key = wo.customer_name || 'No Customer';
          break;
        case 'part':
          key = wo.part_number || 'No Part';
          break;
        case 'status':
          key = wo.status;
          break;
        default:
          key = 'Unknown';
      }
      if (!groups[key]) groups[key] = [];
      groups[key].push(wo);
    });
    
    return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
  }, [filteredWorkOrders, groupBy]);

  // Stats
  const stats = useMemo(() => {
    const overdue = filteredWorkOrders.filter(wo => 
      wo.due_date && new Date(wo.due_date) < new Date() && !['complete', 'closed', 'cancelled'].includes(wo.status)
    ).length;
    const inProgress = filteredWorkOrders.filter(wo => wo.status === 'in_progress').length;
    const dueToday = filteredWorkOrders.filter(wo => {
      if (!wo.due_date) return false;
      const due = new Date(wo.due_date);
      const today = new Date();
      return due.toDateString() === today.toDateString();
    }).length;
    return { overdue, inProgress, dueToday };
  }, [filteredWorkOrders]);

  if (loading) {
    return (
      <div className="space-y-6">
        {/* Header skeleton */}
        <div className="flex items-center justify-between">
          <div className="space-y-2">
            <div className="h-8 w-48 bg-gray-200 rounded animate-pulse" />
            <div className="h-4 w-72 bg-gray-200 rounded animate-pulse" />
          </div>
          <div className="h-10 w-40 bg-gray-200 rounded animate-pulse" />
        </div>
        
        {/* Stats skeleton */}
        <div className="grid grid-cols-3 gap-4">
          {[1, 2, 3].map((i) => (
            <SkeletonCard key={i} className="h-24" />
          ))}
        </div>
        
        {/* Table skeleton */}
        <div className="card overflow-hidden">
          <SkeletonTable rows={8} columns={8} />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="page-header">
        <div>
          <h1 className="page-title">Work Orders</h1>
          <p className="page-subtitle">Manage and track manufacturing orders</p>
        </div>
        <div className="page-actions" data-tour="wo-create">
          <Link to="/work-orders/new" className="btn-primary">
            <PlusIcon className="h-5 w-5 mr-2" />
            New Work Order
          </Link>
        </div>
      </div>

      {/* Quick Stats */}
      <div className="grid grid-cols-3 gap-4">
        <div className="card-compact flex items-center gap-4">
          <div className={`p-3 rounded-xl ${stats.overdue > 0 ? 'bg-red-100' : 'bg-emerald-100'}`}>
            <ExclamationTriangleIcon className={`h-6 w-6 ${stats.overdue > 0 ? 'text-red-600' : 'text-emerald-600'}`} />
          </div>
          <div>
            <p className={`text-2xl font-bold ${stats.overdue > 0 ? 'text-red-600' : 'text-surface-900'}`}>
              {stats.overdue}
            </p>
            <p className="text-sm text-surface-500">Overdue</p>
          </div>
        </div>
        <div className="card-compact flex items-center gap-4">
          <div className="p-3 rounded-xl bg-blue-100">
            <Squares2X2Icon className="h-6 w-6 text-blue-600" />
          </div>
          <div>
            <p className="text-2xl font-bold text-surface-900">{stats.inProgress}</p>
            <p className="text-sm text-surface-500">In Progress</p>
          </div>
        </div>
        <div className="card-compact flex items-center gap-4">
          <div className={`p-3 rounded-xl ${stats.dueToday > 0 ? 'bg-amber-100' : 'bg-surface-100'}`}>
            <ClockIcon className={`h-6 w-6 ${stats.dueToday > 0 ? 'text-amber-600' : 'text-surface-500'}`} />
          </div>
          <div>
            <p className={`text-2xl font-bold ${stats.dueToday > 0 ? 'text-amber-600' : 'text-surface-900'}`}>
              {stats.dueToday}
            </p>
            <p className="text-sm text-surface-500">Due Today</p>
          </div>
        </div>
      </div>

      {/* Filters */}
      <div className="card" data-tour="wo-filters">
        <div className="flex flex-col lg:flex-row gap-4">
          {/* Search */}
          <div className="relative flex-1">
            <MagnifyingGlassIcon className="h-5 w-5 absolute left-4 top-1/2 transform -translate-y-1/2 text-surface-400" />
            <input
              type="text"
              placeholder="Search by WO#, part, or customer..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input pl-11"
            />
          </div>
          
          {/* Status Filter */}
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="input w-full lg:w-44"
          >
            <option value="">All Active</option>
            <option value="draft">Draft</option>
            <option value="released">Released</option>
            <option value="in_progress">In Progress</option>
            <option value="on_hold">On Hold</option>
            <option value="complete">Complete</option>
            <option value="closed">Closed</option>
          </select>
          
          {/* Customer Filter */}
          <select
            value={customerFilter}
            onChange={(e) => setCustomerFilter(e.target.value)}
            className="input w-full lg:w-52"
          >
            <option value="">All Customers</option>
            {customers.map(c => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
          
          {/* Group By */}
          <select
            value={groupBy}
            onChange={(e) => setGroupBy(e.target.value as GroupBy)}
            className="input w-full lg:w-44"
          >
            <option value="none">No Grouping</option>
            <option value="customer">By Customer</option>
            <option value="part">By Part</option>
            <option value="status">By Status</option>
          </select>
        </div>
        
        {/* Toggle Options */}
        <div className="flex items-center justify-between mt-4 pt-4 border-t border-surface-200">
          <label className="flex items-center gap-2 cursor-pointer group">
            <input
              type="checkbox"
              checked={hideCOTS}
              onChange={(e) => setHideCOTS(e.target.checked)}
              className="checkbox"
            />
            <span className="text-sm text-surface-600 group-hover:text-surface-900">Hide COTS/Hardware</span>
          </label>
          <span className="text-sm text-surface-500">
            Showing <span className="font-semibold text-surface-700">{filteredWorkOrders.length}</span> of {workOrders.length} work orders
          </span>
        </div>
      </div>

      {/* Work Orders List */}
      {groupBy !== 'none' && groupedWorkOrders ? (
        // Grouped View
        <div className="space-y-4">
          {groupedWorkOrders.map(([groupName, orders]) => (
            <div key={groupName} className="card card-flush overflow-hidden">
              <div className="bg-surface-50 px-6 py-4 border-b border-surface-200">
                <div className="flex items-center justify-between">
                  <h3 className="font-semibold text-surface-900 capitalize">
                    {groupBy === 'status' ? groupName.replace('_', ' ') : groupName}
                  </h3>
                  <span className="badge badge-neutral">
                    {orders.length} order{orders.length !== 1 ? 's' : ''}
                  </span>
                </div>
              </div>
              <WorkOrderTable 
                workOrders={orders} 
                hideColumn={groupBy === 'customer' ? 'customer' : groupBy === 'part' ? 'part' : undefined}
                onDelete={handleDelete}
              />
            </div>
          ))}
        </div>
      ) : (
        // Flat Table View
        <div className="card card-flush overflow-hidden" data-tour="wo-list">
          <WorkOrderTable workOrders={filteredWorkOrders} onDelete={handleDelete} />
          
          {filteredWorkOrders.length === 0 && (
            <div className="text-center py-16">
              <div className="p-4 rounded-full bg-surface-100 w-fit mx-auto mb-4">
                <ListBulletIcon className="h-8 w-8 text-surface-400" />
              </div>
              <p className="text-surface-600 font-medium">No work orders found</p>
              <p className="text-sm text-surface-500 mt-1">Try adjusting your filters</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Work Order Table Component
interface WorkOrderTableProps {
  workOrders: WorkOrderSummary[];
  hideColumn?: 'customer' | 'part';
  onDelete?: (wo: WorkOrderSummary) => void;
}

function WorkOrderTable({ workOrders, hideColumn, onDelete }: WorkOrderTableProps) {
  const isOverdue = (wo: WorkOrderSummary) => {
    return wo.due_date && new Date(wo.due_date) < new Date() && !['complete', 'closed', 'cancelled'].includes(wo.status);
  };

  return (
    <div className="table-container border-0">
      <table className="table">
        <thead>
          <tr>
            <th>Work Order</th>
            {hideColumn !== 'part' && <th>Part</th>}
            {hideColumn !== 'customer' && <th>Customer</th>}
            <th>Quantity</th>
            <th>Due Date</th>
            <th>Priority</th>
            <th>Status</th>
            <th className="w-12"></th>
          </tr>
        </thead>
        <tbody>
          {workOrders.map((wo) => {
            const status = statusConfig[wo.status] || statusConfig.draft;
            const priority = priorityConfig[wo.priority] || priorityConfig[4];
            const overdue = isOverdue(wo);
            
            return (
              <tr key={wo.id} className={overdue ? 'bg-red-50/50' : ''}>
                <td>
                  <Link 
                    to={`/work-orders/${wo.id}`} 
                    className="font-semibold text-werco-600 hover:text-werco-700 hover:underline"
                  >
                    {wo.work_order_number}
                  </Link>
                </td>
                {hideColumn !== 'part' && (
                  <td>
                    <div>
                      <p className="font-medium text-surface-900">{wo.part_number}</p>
                      <p className="text-sm text-surface-500 line-clamp-1">{wo.part_name}</p>
                    </div>
                  </td>
                )}
                {hideColumn !== 'customer' && (
                  <td className="text-surface-600">{wo.customer_name || '—'}</td>
                )}
                <td>
                  <div className="flex items-center gap-2">
                    <div className="flex-1 h-2 bg-surface-200 rounded-full overflow-hidden w-20">
                      <div 
                        className="h-full bg-werco-500 rounded-full transition-all"
                        style={{ width: `${Math.min(100, (wo.quantity_complete / wo.quantity_ordered) * 100)}%` }}
                      />
                    </div>
                    <span className="text-sm font-medium text-surface-700 tabular-nums">
                      {wo.quantity_complete}/{wo.quantity_ordered}
                    </span>
                  </div>
                </td>
                <td>
                  <span className={`text-sm font-medium ${overdue ? 'text-red-600' : 'text-surface-700'}`}>
                    {wo.due_date ? format(new Date(wo.due_date), 'MMM d, yyyy') : '—'}
                  </span>
                  {overdue && (
                    <span className="ml-2 badge badge-danger text-[10px] py-0.5">OVERDUE</span>
                  )}
                </td>
                <td>
                  <span className={`badge ${priority.bg} ${priority.text}`}>
                    P{wo.priority}
                  </span>
                </td>
                <td>
                  <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold ${status.bg} ${status.text}`}>
                    <span className={`w-1.5 h-1.5 rounded-full ${status.dot}`}></span>
                    {wo.status.replace('_', ' ')}
                  </span>
                </td>
                <td>
                  <div className="flex items-center gap-1">
                    {onDelete && (wo.status === 'draft' || wo.status === 'cancelled') && (
                      <button 
                        onClick={() => onDelete(wo)}
                        className="p-2 rounded-lg text-surface-400 hover:text-red-600 hover:bg-red-50 transition-colors"
                        title="Delete"
                      >
                        <TrashIcon className="h-4 w-4" />
                      </button>
                    )}
                    <Link 
                      to={`/work-orders/${wo.id}`}
                      className="p-2 rounded-lg text-surface-400 hover:text-werco-600 hover:bg-werco-50 transition-colors"
                    >
                      <ChevronRightIcon className="h-5 w-5" />
                    </Link>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
