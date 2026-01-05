import React, { useEffect, useState } from 'react';
import api from '../services/api';
import {
  ChartBarIcon,
  ClockIcon,
  CurrencyDollarIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  TruckIcon,
} from '@heroicons/react/24/outline';

interface ProductionSummary {
  period_days: number;
  work_orders_by_status: Record<string, number>;
  total_completed: number;
  on_time_delivery_count: number;
  on_time_delivery_pct: number;
  total_hours_worked: number;
  total_produced: number;
  total_scrapped: number;
  scrap_rate_pct: number;
}

interface QualityMetrics {
  period_days: number;
  total_ncrs: number;
  open_ncrs: number;
  ncr_by_status: Record<string, number>;
  ncr_by_source: Record<string, number>;
  receiving_total_qty: number;
  receiving_rejected_qty: number;
  receiving_reject_rate_pct: number;
}

interface InventoryValue {
  total_value: number;
  total_quantity: number;
  unique_parts: number;
}

interface VendorPerformance {
  vendor_id: number;
  vendor_code: string;
  vendor_name: string;
  total_ordered: number;
  total_received: number;
  fill_rate_pct: number;
  reject_rate_pct: number;
  po_count: number;
}

interface WorkCenterUtil {
  work_center_id: number;
  work_center_code: string;
  work_center_name: string;
  hours_worked: number;
  available_hours: number;
  utilization_pct: number;
}

interface DailyOutput {
  date: string;
  completed: number;
  scrapped: number;
}

interface WorkOrderCost {
  work_order_id: number;
  work_order_number: string;
  part_number?: string;
  part_name?: string;
  quantity: number;
  status: string;
  customer_name?: string;
  estimated_hours: number;
  estimated_total: number;
  actual_hours: number;
  actual_total: number;
  cost_variance: number;
  variance_pct: number;
}

interface EmployeeTime {
  user_id: number;
  employee_name: string;
  total_hours: number;
  entries: Array<{
    date?: string;
    hours: number;
    work_order_number?: string;
    operation?: string;
    work_center?: string;
  }>;
}

type TabType = 'dashboard' | 'costing' | 'timesheets';

export default function Reports() {
  const [activeTab, setActiveTab] = useState<TabType>('dashboard');
  const [production, setProduction] = useState<ProductionSummary | null>(null);
  const [quality, setQuality] = useState<QualityMetrics | null>(null);
  const [inventory, setInventory] = useState<InventoryValue | null>(null);
  const [vendors, setVendors] = useState<VendorPerformance[]>([]);
  const [utilization, setUtilization] = useState<WorkCenterUtil[]>([]);
  const [dailyOutput, setDailyOutput] = useState<DailyOutput[]>([]);
  const [costing, setCosting] = useState<WorkOrderCost[]>([]);
  const [timesheets, setTimesheets] = useState<EmployeeTime[]>([]);
  const [loading, setLoading] = useState(true);
  const [period, setPeriod] = useState(30);

  useEffect(() => {
    loadData();
  }, [period]);

  const loadData = async () => {
    try {
      const [prodRes, qualRes, invRes, vendRes, utilRes, dailyRes, costRes, timeRes] = await Promise.all([
        api.getProductionSummary(period),
        api.getQualityMetrics(period),
        api.getInventoryValue(),
        api.getVendorPerformance(90),
        api.getWorkCenterUtilization(period),
        api.getDailyOutput(14),
        api.getWorkOrderCosting(undefined, period),
        api.getEmployeeTimeReport()
      ]);
      setProduction(prodRes);
      setQuality(qualRes);
      setInventory(invRes);
      setVendors(vendRes);
      setUtilization(utilRes);
      setDailyOutput(dailyRes);
      setCosting(costRes);
      setTimesheets(timeRes);
    } catch (err) {
      console.error('Failed to load reports:', err);
    } finally {
      setLoading(false);
    }
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
        <h1 className="text-2xl font-bold text-gray-900">Reports & Analytics</h1>
        <select
          value={period}
          onChange={(e) => setPeriod(parseInt(e.target.value))}
          className="input w-40"
        >
          <option value={7}>Last 7 Days</option>
          <option value={30}>Last 30 Days</option>
          <option value={90}>Last 90 Days</option>
        </select>
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200">
        <nav className="flex space-x-8">
          {[
            { id: 'dashboard', label: 'Dashboard' },
            { id: 'costing', label: 'Work Order Costing' },
            { id: 'timesheets', label: 'Employee Time' }
          ].map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id as TabType)}
              className={`py-2 px-1 border-b-2 font-medium text-sm ${
                activeTab === tab.id
                  ? 'border-werco-primary text-werco-primary'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {activeTab === 'dashboard' && (
      <>
      {/* KPI Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="card bg-gradient-to-br from-blue-500 to-blue-600 text-white">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-blue-100 text-sm">On-Time Delivery</p>
              <p className="text-3xl font-bold">{production?.on_time_delivery_pct.toFixed(1)}%</p>
              <p className="text-blue-200 text-sm">{production?.on_time_delivery_count} of {production?.total_completed}</p>
            </div>
            <CheckCircleIcon className="h-12 w-12 text-blue-200" />
          </div>
        </div>

        <div className="card bg-gradient-to-br from-green-500 to-green-600 text-white">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-green-100 text-sm">Hours Worked</p>
              <p className="text-3xl font-bold">{production?.total_hours_worked}</p>
              <p className="text-green-200 text-sm">Last {period} days</p>
            </div>
            <ClockIcon className="h-12 w-12 text-green-200" />
          </div>
        </div>

        <div className="card bg-gradient-to-br from-amber-500 to-amber-600 text-white">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-amber-100 text-sm">Scrap Rate</p>
              <p className="text-3xl font-bold">{production?.scrap_rate_pct.toFixed(2)}%</p>
              <p className="text-amber-200 text-sm">{production?.total_scrapped} scrapped</p>
            </div>
            <ExclamationTriangleIcon className="h-12 w-12 text-amber-200" />
          </div>
        </div>

        <div className="card bg-gradient-to-br from-purple-500 to-purple-600 text-white">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-purple-100 text-sm">Inventory Value</p>
              <p className="text-3xl font-bold">${(inventory?.total_value || 0).toLocaleString()}</p>
              <p className="text-purple-200 text-sm">{inventory?.unique_parts} parts</p>
            </div>
            <CurrencyDollarIcon className="h-12 w-12 text-purple-200" />
          </div>
        </div>
      </div>

      {/* Charts Row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Daily Output Chart */}
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">Daily Production Output</h2>
          <div className="h-64 flex items-end gap-1">
            {dailyOutput.map((day, idx) => {
              const maxVal = Math.max(...dailyOutput.map(d => d.completed + d.scrapped), 1);
              const completedHeight = (day.completed / maxVal) * 100;
              const scrappedHeight = (day.scrapped / maxVal) * 100;
              
              return (
                <div key={idx} className="flex-1 flex flex-col items-center">
                  <div className="w-full flex flex-col-reverse" style={{ height: '200px' }}>
                    <div 
                      className="bg-green-500 rounded-t"
                      style={{ height: `${completedHeight}%` }}
                      title={`Completed: ${day.completed}`}
                    />
                    <div 
                      className="bg-red-400"
                      style={{ height: `${scrappedHeight}%` }}
                      title={`Scrapped: ${day.scrapped}`}
                    />
                  </div>
                  <div className="text-xs text-gray-500 mt-1 transform -rotate-45 origin-top-left">
                    {new Date(day.date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
                  </div>
                </div>
              );
            })}
          </div>
          <div className="flex gap-4 mt-4 justify-center text-sm">
            <span className="flex items-center gap-1">
              <span className="w-3 h-3 bg-green-500 rounded"></span> Completed
            </span>
            <span className="flex items-center gap-1">
              <span className="w-3 h-3 bg-red-400 rounded"></span> Scrapped
            </span>
          </div>
        </div>

        {/* Work Center Utilization */}
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">Work Center Utilization</h2>
          <div className="space-y-3">
            {utilization.slice(0, 8).map((wc) => (
              <div key={wc.work_center_id}>
                <div className="flex justify-between text-sm mb-1">
                  <span className="font-medium">{wc.work_center_code}</span>
                  <span className="text-gray-500">{wc.utilization_pct}%</span>
                </div>
                <div className="h-4 bg-gray-200 rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full ${
                      wc.utilization_pct > 80 ? 'bg-green-500' :
                      wc.utilization_pct > 50 ? 'bg-blue-500' :
                      wc.utilization_pct > 25 ? 'bg-yellow-500' :
                      'bg-red-400'
                    }`}
                    style={{ width: `${Math.min(wc.utilization_pct, 100)}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Quality Metrics */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">Quality Summary</h2>
          <div className="grid grid-cols-2 gap-4">
            <div className="bg-gray-50 rounded-lg p-4 text-center">
              <p className="text-3xl font-bold text-red-600">{quality?.total_ncrs}</p>
              <p className="text-sm text-gray-600">Total NCRs</p>
            </div>
            <div className="bg-gray-50 rounded-lg p-4 text-center">
              <p className="text-3xl font-bold text-orange-600">{quality?.open_ncrs}</p>
              <p className="text-sm text-gray-600">Open NCRs</p>
            </div>
            <div className="bg-gray-50 rounded-lg p-4 text-center">
              <p className="text-3xl font-bold text-blue-600">{quality?.receiving_total_qty}</p>
              <p className="text-sm text-gray-600">Qty Received</p>
            </div>
            <div className="bg-gray-50 rounded-lg p-4 text-center">
              <p className="text-3xl font-bold text-purple-600">{quality?.receiving_reject_rate_pct.toFixed(2)}%</p>
              <p className="text-sm text-gray-600">Receiving Reject Rate</p>
            </div>
          </div>
          {quality && Object.keys(quality.ncr_by_source).length > 0 && (
            <div className="mt-4">
              <h3 className="text-sm font-medium text-gray-700 mb-2">NCRs by Source</h3>
              <div className="flex flex-wrap gap-2">
                {Object.entries(quality.ncr_by_source).map(([source, count]) => (
                  <span key={source} className="px-3 py-1 bg-gray-100 rounded-full text-sm">
                    {source.replace('_', ' ')}: {count}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Vendor Performance */}
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">Top Vendor Performance</h2>
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="border-b">
                  <th className="text-left py-2">Vendor</th>
                  <th className="text-right py-2">POs</th>
                  <th className="text-right py-2">Fill Rate</th>
                  <th className="text-right py-2">Reject Rate</th>
                </tr>
              </thead>
              <tbody>
                {vendors.slice(0, 5).map((v) => (
                  <tr key={v.vendor_id} className="border-b">
                    <td className="py-2 font-medium">{v.vendor_name}</td>
                    <td className="py-2 text-right">{v.po_count}</td>
                    <td className="py-2 text-right">
                      <span className={v.fill_rate_pct >= 90 ? 'text-green-600' : v.fill_rate_pct >= 70 ? 'text-yellow-600' : 'text-red-600'}>
                        {v.fill_rate_pct.toFixed(1)}%
                      </span>
                    </td>
                    <td className="py-2 text-right">
                      <span className={v.reject_rate_pct <= 2 ? 'text-green-600' : v.reject_rate_pct <= 5 ? 'text-yellow-600' : 'text-red-600'}>
                        {v.reject_rate_pct.toFixed(2)}%
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {vendors.length === 0 && (
              <p className="text-center text-gray-500 py-4">No vendor data available</p>
            )}
          </div>
        </div>
      </div>

      {/* Work Orders by Status */}
      <div className="card">
        <h2 className="text-lg font-semibold mb-4">Work Orders by Status</h2>
        <div className="flex flex-wrap gap-4">
          {production && Object.entries(production.work_orders_by_status).map(([status, count]) => (
            <div key={status} className="bg-gray-50 rounded-lg px-6 py-4 text-center min-w-24">
              <p className="text-2xl font-bold">{count}</p>
              <p className="text-sm text-gray-600 capitalize">{status.replace('_', ' ')}</p>
            </div>
          ))}
        </div>
      </div>
      </>
      )}

      {/* Costing Tab */}
      {activeTab === 'costing' && (
        <div className="space-y-6">
          <div className="card">
            <h2 className="text-lg font-semibold mb-4">Work Order Cost Analysis</h2>
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="border-b bg-gray-50">
                    <th className="text-left py-3 px-4">Work Order</th>
                    <th className="text-left py-3 px-4">Part</th>
                    <th className="text-right py-3 px-4">Qty</th>
                    <th className="text-right py-3 px-4">Est Hours</th>
                    <th className="text-right py-3 px-4">Act Hours</th>
                    <th className="text-right py-3 px-4">Est Cost</th>
                    <th className="text-right py-3 px-4">Act Cost</th>
                    <th className="text-right py-3 px-4">Variance</th>
                  </tr>
                </thead>
                <tbody>
                  {costing.map((wo) => (
                    <tr key={wo.work_order_id} className="border-b hover:bg-gray-50">
                      <td className="py-3 px-4">
                        <div className="font-medium text-werco-primary">{wo.work_order_number}</div>
                        <div className="text-xs text-gray-500">{wo.customer_name || '-'}</div>
                      </td>
                      <td className="py-3 px-4">
                        <div>{wo.part_number}</div>
                        <div className="text-xs text-gray-500">{wo.part_name}</div>
                      </td>
                      <td className="py-3 px-4 text-right">{wo.quantity}</td>
                      <td className="py-3 px-4 text-right">{wo.estimated_hours.toFixed(1)}</td>
                      <td className="py-3 px-4 text-right">{wo.actual_hours.toFixed(1)}</td>
                      <td className="py-3 px-4 text-right">${wo.estimated_total.toFixed(2)}</td>
                      <td className="py-3 px-4 text-right">${wo.actual_total.toFixed(2)}</td>
                      <td className={`py-3 px-4 text-right font-medium ${
                        wo.cost_variance > 0 ? 'text-red-600' : wo.cost_variance < 0 ? 'text-green-600' : ''
                      }`}>
                        {wo.cost_variance > 0 ? '+' : ''}${wo.cost_variance.toFixed(2)}
                        <div className="text-xs">({wo.variance_pct.toFixed(1)}%)</div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {costing.length === 0 && (
                <p className="text-center text-gray-500 py-8">No work order costing data available</p>
              )}
            </div>
          </div>

          {/* Cost Summary */}
          {costing.length > 0 && (
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
              <div className="card bg-blue-50 border-blue-200 text-center">
                <p className="text-sm text-blue-600">Total Estimated</p>
                <p className="text-2xl font-bold text-blue-800">
                  ${costing.reduce((sum, wo) => sum + wo.estimated_total, 0).toLocaleString()}
                </p>
              </div>
              <div className="card bg-green-50 border-green-200 text-center">
                <p className="text-sm text-green-600">Total Actual</p>
                <p className="text-2xl font-bold text-green-800">
                  ${costing.reduce((sum, wo) => sum + wo.actual_total, 0).toLocaleString()}
                </p>
              </div>
              <div className="card bg-purple-50 border-purple-200 text-center">
                <p className="text-sm text-purple-600">Total Hours</p>
                <p className="text-2xl font-bold text-purple-800">
                  {costing.reduce((sum, wo) => sum + wo.actual_hours, 0).toFixed(1)}
                </p>
              </div>
              <div className={`card text-center ${
                costing.reduce((sum, wo) => sum + wo.cost_variance, 0) > 0 
                  ? 'bg-red-50 border-red-200' 
                  : 'bg-green-50 border-green-200'
              }`}>
                <p className="text-sm text-gray-600">Total Variance</p>
                <p className={`text-2xl font-bold ${
                  costing.reduce((sum, wo) => sum + wo.cost_variance, 0) > 0 
                    ? 'text-red-800' 
                    : 'text-green-800'
                }`}>
                  ${costing.reduce((sum, wo) => sum + wo.cost_variance, 0).toLocaleString()}
                </p>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Timesheets Tab */}
      {activeTab === 'timesheets' && (
        <div className="space-y-6">
          <div className="card">
            <h2 className="text-lg font-semibold mb-4">Employee Time Report (Last 7 Days)</h2>
            
            {timesheets.map((emp) => (
              <div key={emp.user_id} className="mb-6 last:mb-0">
                <div className="flex justify-between items-center bg-gray-100 px-4 py-2 rounded-t-lg">
                  <span className="font-semibold">{emp.employee_name}</span>
                  <span className="text-werco-primary font-bold">{emp.total_hours} hrs</span>
                </div>
                <div className="border border-t-0 rounded-b-lg overflow-hidden">
                  <table className="min-w-full text-sm">
                    <thead>
                      <tr className="bg-gray-50">
                        <th className="text-left py-2 px-4">Date</th>
                        <th className="text-left py-2 px-4">Work Order</th>
                        <th className="text-left py-2 px-4">Operation</th>
                        <th className="text-left py-2 px-4">Work Center</th>
                        <th className="text-right py-2 px-4">Hours</th>
                      </tr>
                    </thead>
                    <tbody>
                      {emp.entries.slice(0, 10).map((entry, idx) => (
                        <tr key={idx} className="border-t">
                          <td className="py-2 px-4">{entry.date || '-'}</td>
                          <td className="py-2 px-4 font-mono">{entry.work_order_number || '-'}</td>
                          <td className="py-2 px-4">{entry.operation || '-'}</td>
                          <td className="py-2 px-4">{entry.work_center || '-'}</td>
                          <td className="py-2 px-4 text-right">{entry.hours.toFixed(2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
            
            {timesheets.length === 0 && (
              <p className="text-center text-gray-500 py-8">No time entries found</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
