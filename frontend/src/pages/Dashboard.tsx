import React, { useEffect, useState, useCallback, useRef, useMemo } from 'react';
import { Link } from 'react-router-dom';
import api from '../services/api';
import { SkeletonDashboard } from '../components/ui/Skeleton';
import { ActiveAssignment, DashboardData, SignedInUserStatus, WorkCenterStatus } from '../types';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import { formatCentralDate, formatCentralTime, isDateBeforeTodayInCentral } from '../utils/centralTime';
import {
  ClipboardDocumentListIcon,
  ExclamationTriangleIcon,
  CalendarIcon,
  CheckCircleIcon,
  CubeIcon,
  WrenchScrewdriverIcon,
  ShieldExclamationIcon,
  ArrowRightIcon,
  ClockIcon,
  ArrowPathIcon,
  SignalIcon,
  UserGroupIcon,
  UsersIcon,
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

const roleBadgeClasses: Record<string, string> = {
  admin: 'bg-slate-100 text-slate-700',
  manager: 'bg-indigo-100 text-indigo-700',
  supervisor: 'bg-blue-100 text-blue-700',
  operator: 'bg-emerald-100 text-emerald-700',
  quality: 'bg-amber-100 text-amber-700',
  shipping: 'bg-cyan-100 text-cyan-700',
  viewer: 'bg-slate-100 text-slate-600',
};

interface Alert {
  type: 'error' | 'warning' | 'info';
  message: string;
  link?: string;
  icon?: React.ElementType;
}

const formatElapsed = (clockIn: string, nowMs: number) => {
  const startMs = new Date(clockIn).getTime();
  if (Number.isNaN(startMs)) return '--';

  const diffMs = Math.max(nowMs - startMs, 0);
  const totalMinutes = Math.floor(diffMs / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;

  if (hours === 0) {
    return `${minutes}m`;
  }

  return `${hours}h ${minutes}m`;
};

const getRoleBadgeClass = (role?: string) => {
  return roleBadgeClasses[role || 'viewer'] || 'bg-slate-100 text-slate-700';
};

const getEntryTypeLabel = (entryType?: string) => {
  if (!entryType) return 'Run';
  return entryType.charAt(0).toUpperCase() + entryType.slice(1);
};

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
  const [nowMs, setNowMs] = useState(() => Date.now());
  const _refreshTimeoutRef = useRef<NodeJS.Timeout | null>(null); // eslint-disable-line @typescript-eslint/no-unused-vars
  const realtimeRefreshRef = useRef<NodeJS.Timeout | null>(null);
  const realtimeUrl = useMemo(() => {
    const token = getAccessToken();
    return buildWsUrl('/ws/updates', token ? { token } : undefined);
  }, []);

  // Clear "data changed" indicator after 2 seconds
  useEffect(() => {
    if (dataChanged) {
      const timeout = setTimeout(() => setDataChanged(false), 2000);
      return () => clearTimeout(timeout);
    }
  }, [dataChanged]);

  useEffect(() => {
    const interval = setInterval(() => setNowMs(Date.now()), 30000);
    return () => clearInterval(interval);
  }, []);

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

  const scheduleRealtimeRefresh = useCallback(() => {
    if (realtimeRefreshRef.current) return;
    realtimeRefreshRef.current = setTimeout(() => {
      realtimeRefreshRef.current = null;
      loadDashboard(false);
    }, 750);
  }, [loadDashboard]);

  useWebSocket({
    url: realtimeUrl,
    enabled: true,
    onMessage: (message) => {
      if (message.type === 'connected' || message.type === 'ping') return;
      if (['dashboard_update', 'work_order_update', 'shop_floor_update', 'quality_alert', 'notification'].includes(message.type)) {
        scheduleRealtimeRefresh();
      }
    }
  });

  useEffect(() => {
    loadDashboard(true);
    const interval = setInterval(() => loadDashboard(false), 30000);
    return () => clearInterval(interval);
  }, [loadDashboard]);

  useEffect(() => {
    return () => {
      if (realtimeRefreshRef.current) {
        clearTimeout(realtimeRefreshRef.current);
        realtimeRefreshRef.current = null;
      }
    };
  }, []);

  // Manual refresh handler
  const handleManualRefresh = () => {
    if (!isRefreshing) {
      loadDashboard(false);
    }
  };

  const activeAssignments = useMemo(() => {
    return [...(data?.active_assignments || [])].sort((left, right) => {
      const centerCompare = (left.work_center.name || '').localeCompare(right.work_center.name || '');
      if (centerCompare !== 0) return centerCompare;
      return new Date(left.clock_in).getTime() - new Date(right.clock_in).getTime();
    });
  }, [data?.active_assignments]);

  const assignmentsByWorkCenter = useMemo(() => {
    return activeAssignments.reduce<Record<string, ActiveAssignment[]>>((groups, assignment) => {
      const key = assignment.work_center.name || 'Unassigned';
      if (!groups[key]) {
        groups[key] = [];
      }
      groups[key].push(assignment);
      return groups;
    }, {});
  }, [activeAssignments]);

  const signedInUsers = data?.signed_in_users || [];
  const idleSignedInUsers = signedInUsers.filter((user) => !user.has_active_job);

  if (loading) {
    return <SkeletonDashboard />;
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
            <p className="page-subtitle">Live view of shop activity, staffing, and job progress</p>
            {/* Refresh indicator */}
            {isRefreshing && (
              <ArrowPathIcon className="h-4 w-4 text-surface-400 animate-spin" />
            )}
            {lastUpdated && !isRefreshing && (
              <span className="text-xs text-surface-400">
                Updated {formatCentralTime(lastUpdated, { timeZoneName: 'short' })}
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
      <div className="grid grid-cols-2 xl:grid-cols-3 gap-4" data-tour="dashboard-stats">
        <StatCard
          icon={ClipboardDocumentListIcon}
          iconBg="bg-blue-100"
          iconColor="text-blue-600"
          label="Active Work Orders"
          value={data?.summary.active_work_orders || 0}
          href="/work-orders"
        />
        <StatCard
          icon={SignalIcon}
          iconBg="bg-cyan-100"
          iconColor="text-cyan-600"
          label="Signed In Now"
          value={data?.summary.signed_in_users || 0}
          subtitle="Live ERP sessions"
        />
        <StatCard
          icon={UserGroupIcon}
          iconBg="bg-emerald-100"
          iconColor="text-emerald-600"
          label="Checked In Now"
          value={data?.summary.checked_in_users || 0}
          subtitle="Active time entries"
          href="/shop-floor"
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
          icon={UsersIcon}
          iconBg={(data?.summary.idle_signed_in_users || 0) > 0 ? "bg-amber-100" : "bg-slate-100"}
          iconColor={(data?.summary.idle_signed_in_users || 0) > 0 ? "text-amber-600" : "text-slate-600"}
          label="Signed In, Idle"
          value={data?.summary.idle_signed_in_users || 0}
          subtitle="Not clocked into work"
        />
      </div>

      {/* Secondary KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
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

      {/* Live Shop Activity */}
      <div className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">Live Shop Activity</h2>
            <p className="card-subtitle">Who is signed in, who is clocked into work, and what each active job is doing now</p>
          </div>
        </div>

        <div className="mb-4 flex flex-wrap gap-2 text-xs">
          <span className="rounded-full bg-slate-100 px-3 py-1 font-medium text-slate-700">
            Signed in = active authenticated ERP session
          </span>
          <span className="rounded-full bg-emerald-100 px-3 py-1 font-medium text-emerald-700">
            Checked in = active time clock entry
          </span>
        </div>

        {activeAssignments.length > 0 ? (
          <div className="space-y-6">
            {Object.entries(assignmentsByWorkCenter).map(([workCenterName, assignments]) => (
              <div key={workCenterName}>
                <div className="mb-3">
                  <h3 className="text-lg font-semibold text-surface-900">{workCenterName}</h3>
                  <p className="text-sm text-surface-500">
                    {assignments.length} active assignment{assignments.length === 1 ? '' : 's'}
                  </p>
                </div>
                <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                  {assignments.map((assignment) => (
                    <ActiveAssignmentCard
                      key={assignment.time_entry_id}
                      assignment={assignment}
                      nowMs={nowMs}
                    />
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="rounded-2xl border border-dashed border-surface-300 bg-surface-50 px-6 py-12 text-center">
            <UserGroupIcon className="mx-auto h-10 w-10 text-surface-400" />
            <p className="mt-4 text-lg font-medium text-surface-700">No one is clocked into a job right now</p>
            <p className="mt-2 text-sm text-surface-500">
              Signed-in users still appear in the live presence panel, but there are no active time entries right now.
            </p>
          </div>
        )}
      </div>

      {/* Work Center Status */}
      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,2fr)_minmax(320px,1fr)] gap-6">
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
        
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
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
                
                <div className="grid grid-cols-3 gap-3 text-sm">
                  <div>
                    <p className="text-surface-500">Active</p>
                    <p className="font-semibold text-surface-900">{wc.active_operations}</p>
                  </div>
                  <div>
                    <p className="text-surface-500">Queue</p>
                    <p className="font-semibold text-surface-900">{wc.queued_operations}</p>
                  </div>
                  <div>
                    <p className="text-surface-500">People</p>
                    <p className="font-semibold text-surface-900">{wc.active_people_count}</p>
                  </div>
                </div>

                <div className="mt-4 border-t border-white/60 pt-4">
                  <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-surface-500">
                    Active People
                  </p>
                  {wc.active_people.length > 0 ? (
                    <div className="space-y-2">
                      {wc.active_people.map((person) => (
                        <div key={`${wc.id}-${person.user_id}-${person.clock_in}`} className="rounded-lg bg-white/70 p-3">
                          <div className="flex items-center justify-between gap-3">
                            <div>
                              <p className="font-medium text-surface-800">{person.name}</p>
                              <p className="text-xs text-surface-500">{person.employee_id}</p>
                            </div>
                            <p className="text-xs text-surface-500">{formatElapsed(person.clock_in, nowMs)}</p>
                          </div>
                          <p className="mt-2 text-sm text-surface-600">
                            {person.work_order_number} • {person.operation_name}
                          </p>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-sm text-surface-500">No one is clocked into this work center.</p>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">Signed In Right Now</h2>
            <p className="card-subtitle">Live user presence across the ERP</p>
          </div>
        </div>

        {signedInUsers.length ? (
          <div className="space-y-3">
            {signedInUsers.map((user) => (
              <SignedInUserRow key={user.id} user={user} />
            ))}
            {idleSignedInUsers.length > 0 && (
              <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
                <p className="font-semibold">Signed in but not on a job</p>
                <p className="mt-1">{idleSignedInUsers.map((user) => user.name).join(', ')}</p>
              </div>
            )}
          </div>
        ) : (
          <p className="text-sm text-surface-500">No active signed-in sessions detected.</p>
        )}
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

function ActiveAssignmentCard({ assignment, nowMs }: { assignment: ActiveAssignment; nowMs: number }) {
  const orderedQty = Number(assignment.work_order.quantity_ordered || 0);
  const completeQty = Number(assignment.operation.quantity_complete ?? assignment.work_order.quantity_complete ?? 0);
  const progress = orderedQty > 0 ? Math.min(100, Math.round((completeQty / orderedQty) * 100)) : 0;
  const dueDate = assignment.work_order.due_date;
  const isOverdue = Boolean(dueDate && isDateBeforeTodayInCentral(dueDate));

  return (
    <div className="rounded-2xl border border-surface-200 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-lg font-semibold text-surface-900">{assignment.user.name}</h3>
            <span className="rounded-full bg-surface-100 px-2.5 py-1 text-xs font-medium text-surface-600">
              {assignment.user.employee_id}
            </span>
            <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${getRoleBadgeClass(assignment.user.role)}`}>
              {assignment.user.role.replace('_', ' ')}
            </span>
          </div>
          <p className="mt-1 text-sm text-surface-500">
            {assignment.work_center.name}
            {assignment.user.department ? ` • ${assignment.user.department}` : ''}
          </p>
        </div>
        <span className="rounded-full bg-emerald-100 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-emerald-700">
          {getEntryTypeLabel(assignment.entry_type)}
        </span>
      </div>

      <div className="mt-4 rounded-xl bg-slate-50 p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-sm text-surface-500">Work order</p>
            <Link
              to={`/work-orders/${assignment.work_order.id}`}
              className="text-lg font-semibold text-werco-700 hover:text-werco-800"
            >
              {assignment.work_order.work_order_number}
            </Link>
          </div>
          {assignment.work_order.priority ? (
            <span className="rounded-full bg-amber-100 px-3 py-1 text-xs font-semibold text-amber-700">
              Priority {assignment.work_order.priority}
            </span>
          ) : null}
        </div>
        <p className="mt-1 text-sm text-surface-700">
          {assignment.work_order.part_number}
          {assignment.work_order.part_name ? ` • ${assignment.work_order.part_name}` : ''}
        </p>
        <p className="mt-2 text-sm text-surface-600">
          {assignment.operation.operation_number ? `${assignment.operation.operation_number} • ` : ''}
          {assignment.operation.name}
        </p>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 text-sm">
        <div className="rounded-xl border border-surface-200 p-3">
          <p className="text-surface-500">Started</p>
          <p className="mt-1 font-semibold text-surface-900">{formatCentralTime(assignment.clock_in)}</p>
          <p className="mt-1 text-xs text-surface-500">{formatElapsed(assignment.clock_in, nowMs)} elapsed</p>
        </div>
        <div className="rounded-xl border border-surface-200 p-3">
          <p className="text-surface-500">Due</p>
          <p className={`mt-1 font-semibold ${isOverdue ? 'text-red-600' : 'text-surface-900'}`}>
            {dueDate ? formatCentralDate(dueDate, { year: undefined }) : 'No due date'}
          </p>
          <p className="mt-1 text-xs text-surface-500">{assignment.work_order.customer_name || 'No customer specified'}</p>
        </div>
      </div>

      <div className="mt-4">
        <div className="mb-1 flex items-center justify-between text-sm">
          <span className="text-surface-500">Progress</span>
          <span className="font-medium text-surface-700">
            {completeQty}/{orderedQty || 0} ({progress}%)
          </span>
        </div>
        <div className="h-2 rounded-full bg-surface-200">
          <div className="h-2 rounded-full bg-werco-500 transition-all" style={{ width: `${progress}%` }} />
        </div>
      </div>
    </div>
  );
}

function SignedInUserRow({ user }: { user: SignedInUserStatus }) {
  return (
    <div className="rounded-xl border border-surface-200 bg-white p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-semibold text-surface-900">{user.name}</h3>
            <span className="rounded-full bg-surface-100 px-2 py-0.5 text-xs font-medium text-surface-600">
              {user.employee_id}
            </span>
          </div>
          <p className="mt-1 text-sm text-surface-500">
            {user.role.replace('_', ' ')}
            {user.department ? ` • ${user.department}` : ''}
          </p>
        </div>
        <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${user.has_active_job ? 'bg-emerald-100 text-emerald-700' : 'bg-amber-100 text-amber-700'}`}>
          {user.has_active_job ? 'Checked In' : 'Signed In'}
        </span>
      </div>

      <div className="mt-3 space-y-1 text-sm text-surface-600">
        <p>
          Active jobs: <span className="font-medium text-surface-800">{user.active_job_count}</span>
        </p>
        <p>
          Work centers:{' '}
          <span className="font-medium text-surface-800">
            {user.active_work_centers.length ? user.active_work_centers.join(', ') : 'No active assignment'}
          </span>
        </p>
        <p>
          Work orders:{' '}
          <span className="font-medium text-surface-800">
            {user.active_work_orders.length ? user.active_work_orders.join(', ') : 'No active assignment'}
          </span>
        </p>
        <p>
          Connected:{' '}
          <span className="font-medium text-surface-800">
            {user.connected_since ? formatCentralTime(user.connected_since, { timeZoneName: 'short' }) : 'Unknown'}
          </span>
        </p>
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
  href?: string;
}

function StatCard({ icon: Icon, iconBg, iconColor, label, value, valueColor, subtitle, href }: StatCardProps) {
  const content = (
    <div className="stat-card group">
      <div className={`stat-icon ${iconBg} transition-transform group-hover:scale-110`}>
        <Icon className={`h-7 w-7 ${iconColor}`} />
      </div>
      <div className="flex-1 min-w-0">
        <p className="stat-label">{label}</p>
        <p className={`stat-value ${valueColor || ''}`}>{value}</p>
        {subtitle && <p className="text-xs text-surface-400 mt-0.5">{subtitle}</p>}
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
