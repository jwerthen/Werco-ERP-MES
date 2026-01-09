import React, { useEffect, useState, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import api from '../services/api';
import { DashboardData, WorkCenterStatus } from '../types';
import {
  ClipboardDocumentListIcon,
  ExclamationTriangleIcon,
  CalendarIcon,
  CheckCircleIcon,
  CubeIcon,
  WrenchScrewdriverIcon,
  ShieldExclamationIcon,
  ArrowRightIcon,
  ArrowTrendingUpIcon,
  ArrowTrendingDownIcon,
  ClockIcon,
  QrCodeIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline';
import { 
  ExclamationTriangleIcon as ExclamationTriangleSolid,
  CheckCircleIcon as CheckCircleSolid 
} from '@heroicons/react/24/solid';

const workCenterTypeColors: Record<string, string> = {
  fabrication: 'bg-blue-500',
  cnc_machining: 'bg-purple-500',
  laser: 'bg-cyan-500',
  press_brake: 'bg-indigo-500',
  paint: 'bg-amber-500',
  powder_coating: 'bg-orange-500',
  assembly: 'bg-emerald-500',
  welding: 'bg-red-500',
  inspection: 'bg-cyan-500',
  shipping: 'bg-slate-500',
};

const statusColors: Record<string, { bg: string; dot: string; text: string }> = {
  available: { bg: 'bg-emerald-50', dot: 'bg-emerald-500', text: 'text-emerald-700' },
  in_use: { bg: 'bg-blue-50', dot: 'bg-blue-500', text: 'text-blue-700' },
  maintenance: { bg: 'bg-amber-50', dot: 'bg-amber-500', text: 'text-amber-700' },
  offline: { bg: 'bg-red-50', dot: 'bg-red-500', text: 'text-red-700' },
};

interface Alert {
  type: 'error' | 'warning' | 'info';
  message: string;
  link?: string;
  icon?: React.ElementType;
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [openNCRs, setOpenNCRs] = useState(0);
  const [lowInventory, setLowInventory] = useState(0);
  const [equipmentDue, setEquipmentDue] = useState(0);
  
  // Conditional request state
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [dataChanged, setDataChanged] = useState(false);
  const _refreshTimeoutRef = useRef<NodeJS.Timeout | null>(null); // eslint-disable-line @typescript-eslint/no-unused-vars

  // Clear "data changed" indicator after 2 seconds
  useEffect(() => {
    if (dataChanged) {
      const timeout = setTimeout(() => setDataChanged(false), 2000);
      return () => clearTimeout(timeout);
    }
  }, [dataChanged]);

  const loadDashboard = useCallback(async (isInitial = false) => {
    if (!isInitial) {
      setIsRefreshing(true);
    }
    
    try {
      // Use cached request for dashboard (supports ETag/304)
      const [dashboardResult, qualitySummary, equipmentDueData, lowStockData] = await Promise.all([
        api.getDashboardWithCache(),
        api.getQualitySummary().catch(() => ({ open_ncrs: 0 })),
        api.getEquipmentDueSoon(30).catch(() => []),
        api.getLowStockAlerts().catch(() => [])
      ]);
      
      // Only update state if data actually changed (prevents unnecessary re-renders)
      if (dashboardResult.changed || isInitial) {
        setData(dashboardResult.data);
        setDataChanged(!isInitial && dashboardResult.changed);
      }
      
      setOpenNCRs(qualitySummary.open_ncrs || 0);
      setEquipmentDue(equipmentDueData.length || 0);
      setLowInventory(lowStockData.length || 0);
      
      // Update last refreshed timestamp
      if (!dashboardResult.fromCache) {
        setLastUpdated(new Date());
      }
      
      const newAlerts: Alert[] = [];
      const dashboardData = dashboardResult.data;
      
      if (dashboardData.summary.overdue > 0) {
        newAlerts.push({
          type: 'error',
          message: `${dashboardData.summary.overdue} work order(s) are overdue`,
          link: '/work-orders',
          icon: ClockIcon
        });
      }
      if (qualitySummary.open_ncrs > 0) {
        newAlerts.push({
          type: 'warning',
          message: `${qualitySummary.open_ncrs} open NCR(s) require attention`,
          link: '/quality?filter=open',
          icon: ShieldExclamationIcon
        });
      }
      if (equipmentDueData.length > 0) {
        const overdue = equipmentDueData.filter((e: any) => e.days_until_due < 0).length;
        newAlerts.push({
          type: overdue > 0 ? 'error' : 'warning',
          message: overdue > 0 
            ? `${overdue} equipment item(s) overdue for calibration`
            : `${equipmentDueData.length} equipment item(s) due for calibration within 30 days`,
          link: overdue > 0 ? '/calibration?filter=overdue' : '/calibration?filter=due',
          icon: WrenchScrewdriverIcon
        });
      }
      if (lowStockData.length > 0) {
        const critical = lowStockData.filter((i: any) => i.is_critical).length;
        newAlerts.push({
          type: critical > 0 ? 'error' : 'warning',
          message: critical > 0 
            ? `${critical} part(s) at critical inventory levels`
            : `${lowStockData.length} part(s) below reorder point`,
          link: '/inventory?filter=low_stock',
          icon: CubeIcon
        });
      }
      setAlerts(newAlerts);
      setError('');
    } catch (err) {
      setError('Failed to load dashboard data');
    } finally {
      setLoading(false);
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadDashboard(true);
    const interval = setInterval(() => loadDashboard(false), 30000);
    return () => clearInterval(interval);
  }, [loadDashboard]);

  // Manual refresh handler
  const handleManualRefresh = () => {
    if (!isRefreshing) {
      loadDashboard(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <div className="spinner h-12 w-12 mx-auto mb-4"></div>
          <p className="text-surface-500">Loading dashboard...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="alert-danger">
        <ExclamationTriangleSolid className="h-5 w-5 flex-shrink-0" />
        <div>
          <p className="font-medium">Error loading dashboard</p>
          <p className="text-sm opacity-80">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="page-header">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="page-title">Dashboard</h1>
            {/* Data changed indicator */}
            {dataChanged && (
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-100 text-emerald-800 animate-pulse">
                Updated
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <p className="page-subtitle">Manufacturing operations overview</p>
            {/* Refresh indicator */}
            {isRefreshing && (
              <ArrowPathIcon className="h-4 w-4 text-surface-400 animate-spin" />
            )}
            {lastUpdated && !isRefreshing && (
              <span className="text-xs text-surface-400">
                Updated {lastUpdated.toLocaleTimeString()}
              </span>
            )}
          </div>
        </div>
        <div className="page-actions">
          {/* Manual refresh button */}
          <button
            onClick={handleManualRefresh}
            disabled={isRefreshing}
            className="btn-ghost btn-sm"
            title="Refresh dashboard"
          >
            <ArrowPathIcon className={`h-5 w-5 ${isRefreshing ? 'animate-spin' : ''}`} />
          </button>
          <Link to="/scanner" className="btn-secondary">
            <QrCodeIcon className="h-5 w-5 mr-2" />
            Scanner
          </Link>
          <Link to="/shop-floor" className="btn-primary">
            <WrenchScrewdriverIcon className="h-5 w-5 mr-2" />
            Shop Floor
          </Link>
        </div>
      </div>

      {/* Alerts */}
      {alerts.length > 0 && (
        <div className="space-y-2">
          {alerts.map((alert, idx) => {
            const Icon = alert.icon || ExclamationTriangleIcon;
            return (
              <Link
                key={idx}
                to={alert.link || '#'}
                className={`
                  group flex items-center gap-4 p-4 rounded-xl border transition-all duration-200
                  ${alert.type === 'error' 
                    ? 'bg-red-50 border-red-200 text-red-800 hover:bg-red-100' 
                    : alert.type === 'warning'
                    ? 'bg-amber-50 border-amber-200 text-amber-800 hover:bg-amber-100'
                    : 'bg-blue-50 border-blue-200 text-blue-800 hover:bg-blue-100'
                  }
                `}
              >
                <div className={`
                  p-2 rounded-lg
                  ${alert.type === 'error' ? 'bg-red-100' : alert.type === 'warning' ? 'bg-amber-100' : 'bg-blue-100'}
                `}>
                  <Icon className="h-5 w-5" />
                </div>
                <span className="flex-1 font-medium">{alert.message}</span>
                <ArrowRightIcon className="h-5 w-5 opacity-50 group-hover:opacity-100 group-hover:translate-x-1 transition-all" />
              </Link>
            );
          })}
        </div>
      )}

      {/* KPI Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4" data-tour="dashboard-stats">
        <StatCard
          icon={ClipboardDocumentListIcon}
          iconBg="bg-blue-100"
          iconColor="text-blue-600"
          label="Active Work Orders"
          value={data?.summary.active_work_orders || 0}
          href="/work-orders"
        />
        <StatCard
          icon={CalendarIcon}
          iconBg="bg-amber-100"
          iconColor="text-amber-600"
          label="Due Today"
          value={data?.summary.due_today || 0}
          href="/work-orders"
        />
        <StatCard
          icon={ExclamationTriangleIcon}
          iconBg={data?.summary.overdue ? "bg-red-100" : "bg-emerald-100"}
          iconColor={data?.summary.overdue ? "text-red-600" : "text-emerald-600"}
          label="Overdue"
          value={data?.summary.overdue || 0}
          valueColor={data?.summary.overdue ? "text-red-600" : undefined}
          href="/work-orders"
        />
        <StatCard
          icon={ShieldExclamationIcon}
          iconBg={openNCRs > 0 ? "bg-orange-100" : "bg-emerald-100"}
          iconColor={openNCRs > 0 ? "text-orange-600" : "text-emerald-600"}
          label="Open NCRs"
          value={openNCRs}
          valueColor={openNCRs > 0 ? "text-orange-600" : undefined}
          href="/quality"
        />
      </div>

      {/* Secondary KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
        <StatCard
          icon={WrenchScrewdriverIcon}
          iconBg={equipmentDue > 0 ? "bg-amber-100" : "bg-emerald-100"}
          iconColor={equipmentDue > 0 ? "text-amber-600" : "text-emerald-600"}
          label="Calibration Due"
          value={equipmentDue}
          subtitle="Within 30 days"
          href="/calibration"
        />
        <StatCard
          icon={CubeIcon}
          iconBg={lowInventory > 0 ? "bg-red-100" : "bg-emerald-100"}
          iconColor={lowInventory > 0 ? "text-red-600" : "text-emerald-600"}
          label="Low Stock Items"
          value={lowInventory}
          valueColor={lowInventory > 0 ? "text-red-600" : undefined}
          href="/inventory"
        />
        <StatCard
          icon={CheckCircleIcon}
          iconBg="bg-emerald-100"
          iconColor="text-emerald-600"
          label="Completed Today"
          value={data?.recent_completions?.length || 0}
          href="/work-orders"
        />
      </div>

      {/* Work Center Status */}
      <div className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">Work Center Status</h2>
            <p className="card-subtitle">Real-time equipment and station status</p>
          </div>
          <Link to="/work-centers" className="btn-ghost btn-sm">
            View All
          </Link>
        </div>
        
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {data?.work_centers.map((wc: WorkCenterStatus) => {
            const statusStyle = statusColors[wc.status] || statusColors.offline;
            const typeColor = workCenterTypeColors[wc.type] || 'bg-slate-500';
            
            return (
              <div 
                key={wc.id} 
                className={`
                  rounded-xl border border-surface-200 p-4 transition-all duration-200
                  hover:shadow-card-hover hover:border-surface-300
                  ${statusStyle.bg}
                `}
              >
                <div className="flex items-start justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <div className={`w-2.5 h-2.5 rounded-full ${statusStyle.dot} animate-pulse`} />
                    <span className={`text-xs font-semibold uppercase tracking-wide ${statusStyle.text}`}>
                      {wc.status.replace('_', ' ')}
                    </span>
                  </div>
                  <div className={`w-2 h-8 rounded-full ${typeColor}`} />
                </div>
                
                <h3 className="font-semibold text-surface-900 mb-1">{wc.name}</h3>
                <p className="text-xs text-surface-500 capitalize mb-3">
                  {wc.type.replace('_', ' ')}
                </p>
                
                <div className="flex items-center gap-4 text-sm">
                  <div>
                    <span className="text-surface-500">Active: </span>
                    <span className="font-semibold text-surface-900">{wc.active_operations}</span>
                  </div>
                  <div>
                    <span className="text-surface-500">Queue: </span>
                    <span className="font-semibold text-surface-900">{wc.queued_operations}</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Recent Completions */}
      <div className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">Recent Completions</h2>
            <p className="card-subtitle">Latest work order completions</p>
          </div>
          <Link to="/work-orders" className="btn-ghost btn-sm">
            View All
          </Link>
        </div>
        
        {data?.recent_completions.length ? (
          <div className="divide-y divide-surface-100">
            {data.recent_completions.map((completion, index) => (
              <div 
                key={index} 
                className="flex items-center justify-between py-4 first:pt-0 last:pb-0"
              >
                <div className="flex items-center gap-4">
                  <div className="p-2 rounded-lg bg-emerald-100">
                    <CheckCircleSolid className="h-5 w-5 text-emerald-600" />
                  </div>
                  <div>
                    <p className="font-semibold text-surface-900">{completion.work_order_number}</p>
                    <p className="text-sm text-surface-500">Completed</p>
                  </div>
                </div>
                <div className="text-right">
                  <p className="font-semibold text-surface-900 tabular-nums">
                    {completion.quantity_complete} units
                  </p>
                  <p className="text-sm text-surface-500">Quantity</p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-center py-12">
            <div className="p-4 rounded-full bg-surface-100 w-fit mx-auto mb-4">
              <ClipboardDocumentListIcon className="h-8 w-8 text-surface-400" />
            </div>
            <p className="text-surface-500">No recent completions</p>
          </div>
        )}
      </div>
    </div>
  );
}

// Stat Card Component
interface StatCardProps {
  icon: React.ElementType;
  iconBg: string;
  iconColor: string;
  label: string;
  value: number | string;
  valueColor?: string;
  subtitle?: string;
  trend?: { value: number; isUp: boolean };
  href?: string;
}

function StatCard({ icon: Icon, iconBg, iconColor, label, value, valueColor, subtitle, trend, href }: StatCardProps) {
  const content = (
    <div className="stat-card group">
      <div className={`stat-icon ${iconBg} transition-transform group-hover:scale-110`}>
        <Icon className={`h-7 w-7 ${iconColor}`} />
      </div>
      <div className="flex-1 min-w-0">
        <p className="stat-label">{label}</p>
        <p className={`stat-value ${valueColor || ''}`}>{value}</p>
        {subtitle && <p className="text-xs text-surface-400 mt-0.5">{subtitle}</p>}
        {trend && (
          <div className={trend.isUp ? 'stat-trend-up' : 'stat-trend-down'}>
            {trend.isUp ? (
              <ArrowTrendingUpIcon className="h-4 w-4" />
            ) : (
              <ArrowTrendingDownIcon className="h-4 w-4" />
            )}
            <span>{trend.value}%</span>
          </div>
        )}
      </div>
    </div>
  );

  if (href) {
    return (
      <Link to={href} className="block hover:shadow-card-hover transition-shadow rounded-xl">
        {content}
      </Link>
    );
  }

  return content;
}
