import React, { useEffect, useState, useMemo } from 'react';
import { Link } from 'react-router-dom';
import api from '../services/api';
import { WorkOrderSummary, WorkOrderStatus } from '../types';
import { format } from 'date-fns';
import { PlusIcon, MagnifyingGlassIcon, FunnelIcon, Squares2X2Icon, ListBulletIcon } from '@heroicons/react/24/outline';

const statusColors: Record<WorkOrderStatus, string> = {
  draft: 'bg-gray-100 text-gray-800',
  released: 'bg-blue-100 text-blue-800',
  in_progress: 'bg-green-100 text-green-800',
  on_hold: 'bg-yellow-100 text-yellow-800',
  complete: 'bg-emerald-100 text-emerald-800',
  closed: 'bg-gray-100 text-gray-600',
  cancelled: 'bg-red-100 text-red-800',
};

// Part types to exclude (COTS/hardware)
const EXCLUDED_PART_TYPES = ['purchased', 'hardware', 'raw_material'];

type GroupBy = 'none' | 'customer' | 'part';

export default function WorkOrders() {
  const [workOrders, setWorkOrders] = useState<WorkOrderSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [customerFilter, setCustomerFilter] = useState<string>('');
  const [hideCOTS, setHideCOTS] = useState(true);
  const [groupBy, setGroupBy] = useState<GroupBy>('none');

  useEffect(() => {
    loadWorkOrders();
  }, [statusFilter]);

  const loadWorkOrders = async () => {
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
  };

  // Get unique customers for filter dropdown
  const customers = useMemo(() => {
    const unique = new Set(workOrders.map(wo => wo.customer_name).filter(Boolean));
    return Array.from(unique).sort() as string[];
  }, [workOrders]);

  const filteredWorkOrders = useMemo(() => {
    return workOrders.filter(wo => {
      // Hide COTS/hardware parts
      if (hideCOTS && wo.part_type && EXCLUDED_PART_TYPES.includes(wo.part_type)) {
        return false;
      }
      
      // Customer filter
      if (customerFilter && wo.customer_name !== customerFilter) {
        return false;
      }
      
      // Search filter
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

  // Group work orders
  const groupedWorkOrders = useMemo(() => {
    if (groupBy === 'none') return null;
    
    const groups: Record<string, WorkOrderSummary[]> = {};
    filteredWorkOrders.forEach(wo => {
      const key = groupBy === 'customer' 
        ? (wo.customer_name || 'No Customer')
        : (wo.part_number || 'No Part');
      if (!groups[key]) groups[key] = [];
      groups[key].push(wo);
    });
    
    // Sort groups by key
    return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b));
  }, [filteredWorkOrders, groupBy]);

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
        <h1 className="text-2xl font-bold text-gray-900">Work Orders</h1>
        <Link to="/work-orders/new" className="btn-primary flex items-center">
          <PlusIcon className="h-5 w-5 mr-2" />
          New Work Order
        </Link>
      </div>

      {/* Filters */}
      <div className="card p-4">
        <div className="flex flex-col lg:flex-row gap-4">
          {/* Search */}
          <div className="relative flex-1">
            <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 transform -translate-y-1/2 text-gray-400" />
            <input
              type="text"
              placeholder="Search work orders..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input pl-10"
            />
          </div>
          
          {/* Status Filter */}
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="input w-full lg:w-40"
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
            className="input w-full lg:w-48"
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
            className="input w-full lg:w-40"
          >
            <option value="none">No Grouping</option>
            <option value="customer">By Customer</option>
            <option value="part">By Part</option>
          </select>
        </div>
        
        {/* Toggle Options */}
        <div className="flex items-center gap-6 mt-3 pt-3 border-t">
          <label className="flex items-center cursor-pointer">
            <input
              type="checkbox"
              checked={hideCOTS}
              onChange={(e) => setHideCOTS(e.target.checked)}
              className="mr-2 rounded border-gray-300 text-werco-primary focus:ring-werco-primary"
            />
            <span className="text-sm text-gray-700">Hide COTS/Hardware</span>
          </label>
          <span className="text-sm text-gray-500">
            Showing {filteredWorkOrders.length} of {workOrders.length} work orders
          </span>
        </div>
      </div>

      {/* Work Orders - Grouped or Flat */}
      {groupBy !== 'none' && groupedWorkOrders ? (
        // Grouped View
        <div className="space-y-4">
          {groupedWorkOrders.map(([groupName, orders]) => (
            <div key={groupName} className="card overflow-hidden">
              <div className="bg-gray-100 px-4 py-3 border-b">
                <h3 className="font-semibold text-gray-900">
                  {groupName}
                  <span className="ml-2 text-sm font-normal text-gray-500">
                    ({orders.length} work order{orders.length !== 1 ? 's' : ''})
                  </span>
                </h3>
              </div>
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">WO #</th>
                      {groupBy !== 'part' && (
                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                      )}
                      {groupBy !== 'customer' && (
                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Customer</th>
                      )}
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Qty</th>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Due</th>
                      <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {orders.map((wo) => (
                      <tr key={wo.id} className="hover:bg-gray-50">
                        <td className="px-4 py-3">
                          <Link to={`/work-orders/${wo.id}`} className="font-medium text-werco-primary hover:underline">
                            {wo.work_order_number}
                          </Link>
                        </td>
                        {groupBy !== 'part' && (
                          <td className="px-4 py-3 text-sm">{wo.part_number}</td>
                        )}
                        {groupBy !== 'customer' && (
                          <td className="px-4 py-3 text-sm">{wo.customer_name || '-'}</td>
                        )}
                        <td className="px-4 py-3 text-sm">
                          {wo.quantity_complete}/{wo.quantity_ordered}
                        </td>
                        <td className="px-4 py-3 text-sm">
                          {wo.due_date ? format(new Date(wo.due_date), 'MMM d') : '-'}
                        </td>
                        <td className="px-4 py-3">
                          <span className={`inline-flex px-2 py-1 rounded-full text-xs font-medium ${statusColors[wo.status]}`}>
                            {wo.status.replace('_', ' ')}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      ) : (
        // Flat Table View
        <div className="card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">WO #</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Customer</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Qty</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Due Date</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Priority</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {filteredWorkOrders.map((wo) => (
                  <tr key={wo.id} className="hover:bg-gray-50">
                    <td className="px-4 py-4">
                      <Link to={`/work-orders/${wo.id}`} className="font-medium text-werco-primary hover:underline">
                        {wo.work_order_number}
                      </Link>
                    </td>
                    <td className="px-4 py-4">
                      <div>
                        <div className="font-medium">{wo.part_number}</div>
                        <div className="text-sm text-gray-500">{wo.part_name}</div>
                      </div>
                    </td>
                    <td className="px-4 py-4 text-sm">{wo.customer_name || '-'}</td>
                    <td className="px-4 py-4">
                      <span className="font-medium">{wo.quantity_complete}</span>
                      <span className="text-gray-500">/{wo.quantity_ordered}</span>
                    </td>
                    <td className="px-4 py-4 text-sm">
                      {wo.due_date ? format(new Date(wo.due_date), 'MMM d, yyyy') : '-'}
                    </td>
                    <td className="px-4 py-4">
                      <span className={`inline-flex items-center justify-center w-8 h-8 rounded-full text-sm font-bold ${
                        wo.priority <= 2 ? 'bg-red-100 text-red-800' :
                        wo.priority <= 5 ? 'bg-yellow-100 text-yellow-800' :
                        'bg-gray-100 text-gray-800'
                      }`}>
                        {wo.priority}
                      </span>
                    </td>
                    <td className="px-4 py-4">
                      <span className={`inline-flex px-2 py-1 rounded-full text-xs font-medium ${statusColors[wo.status]}`}>
                        {wo.status.replace('_', ' ')}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          
          {filteredWorkOrders.length === 0 && (
            <div className="text-center py-8 text-gray-500">
              No work orders found
            </div>
          )}
        </div>
      )}
    </div>
  );
}
