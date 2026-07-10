import React, { useEffect, useState, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { formatCentralDate } from '../utils/centralTime';
import { MiniStat, MiniStatStrip, CockpitPanel } from '../components/cockpit';
import { EmptyState, ErrorState } from '../components/ui';
import ShipOtdReport from '../components/reports/ShipOtdReport';
import {
  ClockIcon,
  CurrencyDollarIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  InboxArrowDownIcon,
  ScaleIcon,
  BuildingStorefrontIcon,
  TableCellsIcon,
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
  completed_operations?: number;
  quantity_produced?: number;
  quantity_scrapped?: number;
  entries: Array<{
    date?: string;
    clock_in?: string;
    clock_out?: string;
    hours: number;
    work_order_number?: string;
    operation?: string;
    work_center?: string;
    quantity_produced?: number;
    quantity_scrapped?: number;
    completed_at?: string;
    source?: string;
  }>;
}

type TabType = 'dashboard' | 'ship-otd' | 'costing' | 'timesheets';

const TAB_IDS: TabType[] = ['dashboard', 'ship-otd', 'costing', 'timesheets'];

export default function Reports() {
  // Active tab lives in the URL (?tab=) so deep links (e.g. the Analytics
  // "OTD (shipped)" tile) and reloads land on the right report.
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTab, setActiveTabState] = useState<TabType>(() => {
    const fromUrl = searchParams.get('tab') as TabType | null;
    return fromUrl && TAB_IDS.includes(fromUrl) ? fromUrl : 'dashboard';
  });
  const setActiveTab = (tab: TabType) => {
    setActiveTabState(tab);
    if (tab === 'dashboard') setSearchParams({});
    else setSearchParams({ tab });
  };
  const [production, setProduction] = useState<ProductionSummary | null>(null);
  const [quality, setQuality] = useState<QualityMetrics | null>(null);
  const [inventory, setInventory] = useState<InventoryValue | null>(null);
  const [vendors, setVendors] = useState<VendorPerformance[]>([]);
  const [utilization, setUtilization] = useState<WorkCenterUtil[]>([]);
  const [dailyOutput, setDailyOutput] = useState<DailyOutput[]>([]);
  const [costing, setCosting] = useState<WorkOrderCost[]>([]);
  const [timesheets, setTimesheets] = useState<EmployeeTime[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [period, setPeriod] = useState(30);

  const loadData = useCallback(async () => {
    setLoadError(false);
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
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [period]);

  useEffect(() => {
    loadData();
  }, [loadData]);

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
        <h1 className="text-2xl font-bold text-white">Reports & Analytics</h1>
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
      <div className="border-b border-fd-line">
        <nav className="flex space-x-8">
          {[
            { id: 'dashboard', label: 'Dashboard' },
            { id: 'ship-otd', label: 'Ship OTD' },
            { id: 'costing', label: 'Work Order Costing' },
            { id: 'timesheets', label: 'Employee Time' }
          ].map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id as TabType)}
              className={`py-2 px-1 border-b-2 font-medium text-sm ${
                activeTab === tab.id
                  ? 'border-werco-primary text-werco-primary'
                  : 'border-transparent text-slate-400 hover:text-slate-300'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {loadError && (
        <ErrorState
          message="Could not load reports & analytics data."
          onRetry={loadData}
        />
      )}

      {!loadError && activeTab === 'dashboard' && (
      <>
      {/* KPI strip — production + quality + inventory headline metrics.
          Scrap is shown here as the aggregate rate; the per-day scrapped
          counts are rendered canonically in the Daily Output panel below. */}
      <MiniStatStrip className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7 gap-2">
        <MiniStat
          icon={CheckCircleIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="On-Time Delivery"
          value={`${production?.on_time_delivery_pct.toFixed(1) ?? '0.0'}%`}
          subtitle={`${production?.on_time_delivery_count ?? 0} of ${production?.total_completed ?? 0}`}
        />
        <MiniStat
          icon={ClockIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Hours Worked"
          value={production?.total_hours_worked ?? 0}
          subtitle={`Last ${period} days`}
        />
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg="bg-fd-amber/15"
          iconColor="text-fd-amber"
          label="Scrap Rate"
          value={`${production?.scrap_rate_pct.toFixed(2) ?? '0.00'}%`}
          subtitle="see Daily Output"
        />
        <MiniStat
          icon={CurrencyDollarIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="Inventory Value"
          value={`$${(inventory?.total_value || 0).toLocaleString()}`}
          subtitle={`${inventory?.unique_parts ?? 0} parts`}
        />
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg="bg-fd-red/15"
          iconColor="text-fd-red"
          label="Total NCRs"
          value={quality?.total_ncrs ?? 0}
          valueColor="text-fd-red"
          subtitle={`${quality?.open_ncrs ?? 0} open`}
        />
        <MiniStat
          icon={InboxArrowDownIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Qty Received"
          value={quality?.receiving_total_qty ?? 0}
          subtitle={`${quality?.receiving_rejected_qty ?? 0} rejected`}
        />
        <MiniStat
          icon={ScaleIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="Recv Reject Rate"
          value={`${quality?.receiving_reject_rate_pct.toFixed(2) ?? '0.00'}%`}
          subtitle="receiving"
        />
      </MiniStatStrip>

      {/* Cockpit grid: charts + tables side-by-side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-12 gap-4 items-start">
        {/* Daily Output Chart — canonical render of per-day completed/scrapped. */}
        <CockpitPanel
          title="Daily Production Output"
          subtitle="Last 14 days"
          className="xl:col-span-7"
          bodyClassName="lg:max-h-none"
        >
          <div className="h-64 flex items-end gap-1">
            {dailyOutput.map((day, idx) => {
              const maxVal = Math.max(...dailyOutput.map(d => d.completed + d.scrapped), 1);
              const completedHeight = (day.completed / maxVal) * 100;
              const scrappedHeight = (day.scrapped / maxVal) * 100;

              return (
                <div key={idx} className="flex-1 flex flex-col items-center min-w-0">
                  <div className="w-full flex flex-col-reverse" style={{ height: '200px' }}>
                    <div
                      className="bg-fd-green rounded-t-sm"
                      style={{ height: `${completedHeight}%` }}
                      title={`Completed: ${day.completed}`}
                    />
                    <div
                      className="bg-fd-red"
                      style={{ height: `${scrappedHeight}%` }}
                      title={`Scrapped: ${day.scrapped}`}
                    />
                  </div>
                  <div className="text-xs text-slate-400 mt-1 transform -rotate-45 origin-top-left">
                    {formatCentralDate(day.date, { month: 'short', day: 'numeric', year: undefined })}
                  </div>
                </div>
              );
            })}
          </div>
          <div className="flex gap-4 mt-4 justify-center text-sm">
            <span className="flex items-center gap-1">
              <span className="w-3 h-3 bg-fd-green rounded-sm"></span> Completed
            </span>
            <span className="flex items-center gap-1">
              <span className="w-3 h-3 bg-fd-red rounded-sm"></span> Scrapped
            </span>
          </div>
        </CockpitPanel>

        {/* Work Center Utilization */}
        <CockpitPanel
          title="Work Center Utilization"
          subtitle="Top 8 by hours"
          className="xl:col-span-5"
        >
          <div className="space-y-3">
            {utilization.slice(0, 8).map((wc) => (
              <div key={wc.work_center_id} className="min-w-0">
                <div className="flex justify-between text-sm mb-1 gap-2">
                  <span className="font-medium truncate">{wc.work_center_code}</span>
                  <span className="text-slate-400 tabular-nums flex-shrink-0">{wc.utilization_pct}%</span>
                </div>
                <div className="h-4 bg-fd-sunken rounded-sm overflow-hidden">
                  <div
                    className={`h-full rounded-sm ${
                      wc.utilization_pct > 80 ? 'bg-fd-green' :
                      wc.utilization_pct > 50 ? 'bg-fd-blue' :
                      wc.utilization_pct > 25 ? 'bg-fd-amber' :
                      'bg-fd-red'
                    }`}
                    style={{ width: `${Math.min(wc.utilization_pct, 100)}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        </CockpitPanel>

        {/* Top Vendor Performance */}
        <CockpitPanel
          title="Top Vendor Performance"
          subtitle="Last 90 days"
          className="xl:col-span-7"
        >
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="border-b border-fd-line">
                  <th className="text-left py-2">Vendor</th>
                  <th className="text-right py-2">POs</th>
                  <th className="text-right py-2">Fill Rate</th>
                  <th className="text-right py-2">Reject Rate</th>
                </tr>
              </thead>
              <tbody>
                {vendors.slice(0, 5).map((v) => (
                  <tr key={v.vendor_id} className="border-b border-fd-line">
                    <td className="py-2 font-medium truncate max-w-[10rem]">{v.vendor_name}</td>
                    <td className="py-2 text-right tabular-nums">{v.po_count}</td>
                    <td className="py-2 text-right tabular-nums">
                      <span className={v.fill_rate_pct >= 90 ? 'text-fd-green' : v.fill_rate_pct >= 70 ? 'text-fd-amber' : 'text-fd-red'}>
                        {v.fill_rate_pct.toFixed(1)}%
                      </span>
                    </td>
                    <td className="py-2 text-right tabular-nums">
                      <span className={v.reject_rate_pct <= 2 ? 'text-fd-green' : v.reject_rate_pct <= 5 ? 'text-fd-amber' : 'text-fd-red'}>
                        {v.reject_rate_pct.toFixed(2)}%
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {vendors.length === 0 && (
              <EmptyState
                icon={BuildingStorefrontIcon}
                title="No vendor data"
                description="Vendor performance appears once purchase orders are received in this period."
              />
            )}
          </div>
        </CockpitPanel>

        {/* Quality breakdown — NCRs by source + Work Orders by status.
            Headline NCR/receiving counts live in the KPI strip above; this
            panel carries only the per-category breakdowns. */}
        <CockpitPanel
          title="Quality & Work Orders"
          subtitle="By source / status"
          className="xl:col-span-5"
        >
          {quality && Object.keys(quality.ncr_by_source).length > 0 && (
            <div className="mb-3">
              <h3 className="text-[10px] font-medium uppercase tracking-wide text-slate-500 mb-2">NCRs by Source</h3>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(quality.ncr_by_source).map(([source, count]) => (
                  <span key={source} className="px-2 py-0.5 bg-fd-sunken rounded-sm text-xs tabular-nums">
                    {source.replace('_', ' ')}: {count}
                  </span>
                ))}
              </div>
            </div>
          )}
          <div>
            <h3 className="text-[10px] font-medium uppercase tracking-wide text-slate-500 mb-2">Work Orders by Status</h3>
            <div className="flex flex-wrap gap-2">
              {production && Object.entries(production.work_orders_by_status).map(([status, count]) => (
                <div key={status} className="bg-fd-sunken rounded-sm px-3 py-2 text-center min-w-[5rem]">
                  <p className="text-xl font-bold tabular-nums">{count}</p>
                  <p className="text-[10px] text-slate-400 capitalize">{status.replace('_', ' ')}</p>
                </div>
              ))}
            </div>
          </div>
        </CockpitPanel>
      </div>
      </>
      )}

      {/* Ship OTD Tab (Lean Phase 1) — owns its own load/error state, so it
          renders independently of the dashboard fetch above. */}
      {activeTab === 'ship-otd' && <ShipOtdReport periodDays={period} />}

      {/* Costing Tab */}
      {!loadError && activeTab === 'costing' && (
        <div className="space-y-4">
          {/* Cost Summary — collapsed into a MiniStat row above the table. */}
          {costing.length > 0 && (() => {
            const totalEst = costing.reduce((sum, wo) => sum + wo.estimated_total, 0);
            const totalAct = costing.reduce((sum, wo) => sum + wo.actual_total, 0);
            const totalHrs = costing.reduce((sum, wo) => sum + wo.actual_hours, 0);
            const totalVar = costing.reduce((sum, wo) => sum + wo.cost_variance, 0);
            return (
              <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
                <MiniStat
                  icon={CurrencyDollarIcon}
                  iconBg="bg-fd-blue/15"
                  iconColor="text-fd-blue"
                  label="Total Estimated"
                  value={`$${totalEst.toLocaleString()}`}
                />
                <MiniStat
                  icon={CurrencyDollarIcon}
                  iconBg="bg-fd-green/15"
                  iconColor="text-fd-green"
                  label="Total Actual"
                  value={`$${totalAct.toLocaleString()}`}
                />
                <MiniStat
                  icon={ClockIcon}
                  iconBg="bg-fd-cyan/15"
                  iconColor="text-fd-cyan"
                  label="Total Hours"
                  value={totalHrs.toFixed(1)}
                />
                <MiniStat
                  icon={ScaleIcon}
                  iconBg={totalVar > 0 ? 'bg-fd-red/15' : 'bg-fd-green/15'}
                  iconColor={totalVar > 0 ? 'text-fd-red' : 'text-fd-green'}
                  label="Total Variance"
                  value={`$${totalVar.toLocaleString()}`}
                  valueColor={totalVar > 0 ? 'text-fd-red' : 'text-fd-green'}
                />
              </MiniStatStrip>
            );
          })()}

          <CockpitPanel
            title="Work Order Cost Analysis"
            subtitle={`Last ${period} days`}
            footer={costing.length ? `${costing.length} work orders` : undefined}
            bodyClassName="lg:max-h-none"
          >
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="border-b border-fd-line bg-fd-sunken">
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
                    <tr key={wo.work_order_id} className="border-b border-fd-line hover:bg-fd-sunken">
                      <td className="py-3 px-4">
                        <div className="font-medium text-werco-primary">{wo.work_order_number}</div>
                        <div className="text-xs text-slate-400 truncate max-w-[12rem]">{wo.customer_name || '-'}</div>
                      </td>
                      <td className="py-3 px-4">
                        <div className="truncate max-w-[12rem]">{wo.part_number}</div>
                        <div className="text-xs text-slate-400 truncate max-w-[12rem]">{wo.part_name}</div>
                      </td>
                      <td className="py-3 px-4 text-right tabular-nums">{wo.quantity}</td>
                      <td className="py-3 px-4 text-right tabular-nums">{wo.estimated_hours.toFixed(1)}</td>
                      <td className="py-3 px-4 text-right tabular-nums">{wo.actual_hours.toFixed(1)}</td>
                      <td className="py-3 px-4 text-right tabular-nums">${wo.estimated_total.toFixed(2)}</td>
                      <td className="py-3 px-4 text-right tabular-nums">${wo.actual_total.toFixed(2)}</td>
                      <td className={`py-3 px-4 text-right font-medium tabular-nums ${
                        wo.cost_variance > 0 ? 'text-fd-red' : wo.cost_variance < 0 ? 'text-fd-green' : ''
                      }`}>
                        {wo.cost_variance > 0 ? '+' : ''}${wo.cost_variance.toFixed(2)}
                        <div className="text-xs">({wo.variance_pct.toFixed(1)}%)</div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {costing.length === 0 && (
                <EmptyState
                  icon={CurrencyDollarIcon}
                  title="No costing data"
                  description="Work order cost analysis appears once work orders accrue time in this period."
                />
              )}
            </div>
          </CockpitPanel>
        </div>
      )}

      {/* Timesheets Tab */}
      {!loadError && activeTab === 'timesheets' && (
        <div className="space-y-6">
          <div className="card">
            <h2 className="text-lg font-semibold mb-4">Employee Time Report (Last 7 Days)</h2>

            {timesheets.map((emp) => (
              <div key={emp.user_id} className="mb-6 last:mb-0">
                <div className="flex justify-between items-center bg-fd-sunken px-4 py-2 rounded-t-sm">
                  <span className="font-semibold truncate">{emp.employee_name}</span>
                  <div className="flex flex-wrap items-center justify-end gap-3 text-sm">
                    <span className="text-emerald-400 font-bold tabular-nums">{emp.completed_operations || 0} ops</span>
                    <span className="text-slate-300 font-semibold tabular-nums">{emp.quantity_produced || 0} qty</span>
                    <span className="text-werco-primary font-bold tabular-nums">{emp.total_hours} hrs</span>
                  </div>
                </div>
                <div className="border border-t-0 border-fd-line rounded-b-sm overflow-hidden">
                  <table className="min-w-full text-sm">
                    <thead>
                      <tr className="bg-fd-sunken">
                        <th className="text-left py-2 px-4">Date</th>
                        <th className="text-left py-2 px-4">Work Order</th>
                        <th className="text-left py-2 px-4">Operation</th>
                        <th className="text-left py-2 px-4">Work Center</th>
                        <th className="text-right py-2 px-4">Qty</th>
                        <th className="text-left py-2 px-4">Completed</th>
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
                          <td className="py-2 px-4 text-right tabular-nums">{entry.quantity_produced || 0}</td>
                          <td className="py-2 px-4">
                            {entry.completed_at ? formatCentralDate(entry.completed_at, { month: 'short', day: 'numeric' }) : '-'}
                          </td>
                          <td className="py-2 px-4 text-right">{entry.hours.toFixed(2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
            
            {timesheets.length === 0 && (
              <EmptyState
                icon={TableCellsIcon}
                title="No time entries"
                description="Employee time entries from the last 7 days will appear here."
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
