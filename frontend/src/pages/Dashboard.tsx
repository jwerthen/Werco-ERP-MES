import React, { useEffect, useState, useCallback, useRef, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { addDays } from 'date-fns';
import api from '../services/api';
import { SkeletonDashboard } from '../components/ui/Skeleton';
import { MiniStat, CockpitPanel } from '../components/cockpit';
import { ActiveAssignment, DashboardData, SignedInUserStatus, WorkCenterStatus } from '../types';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
import {
  formatCentralDate,
  formatInCentralTime,
  formatCentralTime,
  getCentralDateStamp,
  getCentralTodayDate,
  isDateBeforeTodayInCentral,
} from '../utils/centralTime';
import {
  ClipboardDocumentListIcon,
  ExclamationTriangleIcon,
  CalendarIcon,
  CheckCircleIcon,
  CubeIcon,
  WrenchScrewdriverIcon,
  ShieldExclamationIcon,
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

// Foundry tactical palette — no purple/indigo/orange; blue · cyan · green · amber · red
const workCenterTypeColors: Record<string, string> = {
  fabrication: 'bg-fd-blue',
  cnc_machining: 'bg-fd-cyan',
  laser: 'bg-blue-600',
  press_brake: 'bg-sky-500',
  paint: 'bg-fd-amber',
  powder_coating: 'bg-amber-600',
  assembly: 'bg-fd-green',
  welding: 'bg-fd-red',
  inspection: 'bg-fd-blue',
  shipping: 'bg-slate-600',
};

const statusColors: Record<string, { bg: string; dot: string; text: string }> = {
  available: { bg: 'bg-fd-green/10', dot: 'bg-fd-green', text: 'text-fd-green' },
  in_use: { bg: 'bg-fd-blue/10', dot: 'bg-fd-blue', text: 'text-fd-blue' },
  maintenance: { bg: 'bg-fd-amber/10', dot: 'bg-fd-amber', text: 'text-fd-amber' },
  offline: { bg: 'bg-fd-red/10', dot: 'bg-fd-red', text: 'text-fd-red' },
};

const roleBadgeClasses: Record<string, string> = {
  admin: 'bg-slate-700/40 text-slate-300',
  manager: 'bg-fd-cyan/15 text-fd-cyan',
  supervisor: 'bg-fd-blue/15 text-fd-blue',
  operator: 'bg-fd-green/15 text-fd-green',
  quality: 'bg-fd-amber/15 text-fd-amber',
  shipping: 'bg-fd-blue/15 text-fd-blue',
  viewer: 'bg-slate-700/40 text-slate-400',
};

interface Alert {
  type: 'error' | 'warning' | 'info';
  message: string;
  link?: string;
  icon?: React.ElementType;
}

interface CapacityHeatmapDay {
  date: string;
  scheduled_hours: number;
  capacity_hours: number;
  utilization_pct: number;
  job_count: number;
  overloaded: boolean;
}

interface CapacityHeatmapRow {
  work_center_id: number;
  work_center_code: string;
  work_center_name: string;
  capacity_hours_per_day: number;
  days: CapacityHeatmapDay[];
}

interface CapacityHeatmapResponse {
  start_date: string;
  end_date: string;
  overload_cells: number;
  overloaded_work_centers: number[];
  work_centers: CapacityHeatmapRow[];
}

interface MachineCapacityOverview extends CapacityHeatmapRow {
  scheduled_hours: number;
  capacity_hours: number;
  utilization_pct: number;
  overloaded_days: number;
  available_hours: number;
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
  return roleBadgeClasses[role || 'viewer'] || 'bg-slate-800/50 text-slate-300';
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
  const [capacityHeatmap, setCapacityHeatmap] = useState<CapacityHeatmapResponse | null>(null);

  // Conditional request state
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [dataChanged, setDataChanged] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
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
      // Use cached request for dashboard (supports ETag/304). Widget data
      // is fetched in parallel; individual widget failures degrade to a
      // safe default rather than taking the whole dashboard down. Log
      // the underlying error so failures are observable during triage
      // instead of silently disappearing.
      const logAndFallback = <T,>(widget: string, fallback: T) => (err: unknown): T => {

        console.error(`Dashboard widget "${widget}" failed to load:`, err);
        return fallback;
      };
      const today = getCentralTodayDate();
      const capacityStart = getCentralDateStamp(today);
      const capacityEnd = getCentralDateStamp(addDays(today, 6));
      const [dashboardResult, qualitySummary, equipmentDueData, lowStockData, capacityData] = await Promise.all([
        api.getDashboardWithCache(),
        api.getQualitySummary().catch(logAndFallback('quality summary', { open_ncrs: 0 })),
        api.getEquipmentDueSoon(30).catch(logAndFallback('equipment due soon', [])),
        api.getLowStockAlerts().catch(logAndFallback('low stock alerts', [])),
        api.getCapacityHeatmap(capacityStart, capacityEnd).catch(logAndFallback('capacity heatmap', null))
      ]);

      // Only update state if data actually changed (prevents unnecessary re-renders)
      if (dashboardResult.changed || isInitial) {
        setData(dashboardResult.data);
        setDataChanged(!isInitial && dashboardResult.changed);
      }

      setOpenNCRs(qualitySummary.open_ncrs || 0);
      setEquipmentDue(equipmentDueData.length || 0);
      setLowInventory(lowStockData.length || 0);
      setCapacityHeatmap(capacityData);

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
          link: '/warehouse?tab=inventory&filter=low_stock',
          icon: CubeIcon
        });
      }
      setAlerts(newAlerts);
      setError('');
    } catch {
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
  const onJobSignedInUsers = signedInUsers.filter((user) => user.has_active_job);
  const timeEntryByUserId = useMemo(() => {
    const map = new Map<number, number>();
    activeAssignments.forEach((assignment) => {
      if (!map.has(assignment.user.id)) {
        map.set(assignment.user.id, assignment.time_entry_id);
      }
    });
    return map;
  }, [activeAssignments]);
  const machineCapacityOverview = useMemo<MachineCapacityOverview[]>(() => {
    return (capacityHeatmap?.work_centers || [])
      .map((row) => {
        const scheduledHours = row.days.reduce((sum, day) => sum + day.scheduled_hours, 0);
        const capacityHours = row.days.reduce((sum, day) => sum + day.capacity_hours, 0);
        const utilizationPct = capacityHours > 0 ? (scheduledHours / capacityHours) * 100 : 0;
        return {
          ...row,
          scheduled_hours: scheduledHours,
          capacity_hours: capacityHours,
          utilization_pct: utilizationPct,
          overloaded_days: row.days.filter((day) => day.overloaded).length,
          available_hours: Math.max(0, capacityHours - scheduledHours),
        };
      })
      .sort((a, b) => b.utilization_pct - a.utilization_pct);
  }, [capacityHeatmap]);
  const totalCapacityHours = machineCapacityOverview.reduce((sum, machine) => sum + machine.capacity_hours, 0);
  const totalScheduledHours = machineCapacityOverview.reduce((sum, machine) => sum + machine.scheduled_hours, 0);
  const totalCapacityUtilization = totalCapacityHours > 0 ? (totalScheduledHours / totalCapacityHours) * 100 : 0;

  const capacityDayClass = (day: CapacityHeatmapDay) => {
    if (day.utilization_pct > 100) return 'bg-red-500 text-white border-red-400';
    if (day.utilization_pct >= 90) return 'bg-amber-500 text-slate-950 border-amber-300';
    if (day.utilization_pct >= 70) return 'bg-fd-amber text-slate-950 border-yellow-200';
    if (day.scheduled_hours > 0) return 'bg-emerald-500 text-slate-950 border-emerald-300';
    return 'bg-slate-700 text-slate-300 border-slate-600';
  };

  const capacityStatusLabel = (day: CapacityHeatmapDay) => {
    if (day.utilization_pct > 100) return 'Over capacity';
    if (day.utilization_pct >= 90) return 'Near full';
    if (day.utilization_pct >= 70) return 'Busy';
    if (day.scheduled_hours > 0) return 'Scheduled';
    return 'Open';
  };

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
    <div className="space-y-4">
      {/* Page header */}
      <div className="page-header">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="page-title">Dashboard</h1>
            {/* Data changed indicator */}
            {dataChanged && (
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-fd-green/15 text-emerald-300 animate-pulse">
                Updated
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <p className="page-subtitle">Live view of shop activity, staffing, and job progress</p>
            {/* Refresh indicator */}
            {isRefreshing && (
              <ArrowPathIcon className="h-4 w-4 text-slate-500 animate-spin" />
            )}
            {lastUpdated && !isRefreshing && (
              <span className="text-xs text-slate-500">
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

      {/* Alerts — compact chip row */}
      {alerts.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {alerts.map((alert, idx) => {
            const Icon = alert.icon || ExclamationTriangleIcon;
            return (
              <Link
                key={idx}
                to={alert.link || '#'}
                className={`inline-flex items-center gap-1.5 rounded-sm border px-2.5 py-1 text-xs font-medium transition-colors ${
                  alert.type === 'error'
                    ? 'bg-red-500/10 border-red-500/30 text-red-300 hover:bg-fd-red/20'
                    : alert.type === 'warning'
                    ? 'bg-amber-500/10 border-amber-500/30 text-amber-300 hover:bg-fd-amber/20'
                    : 'bg-blue-500/10 border-blue-500/30 text-blue-300 hover:bg-blue-500/20'
                }`}
              >
                <Icon className="h-4 w-4 flex-shrink-0" />
                <span>{alert.message}</span>
              </Link>
            );
          })}
        </div>
      )}

      {/* KPI strip — one dense row of compact tiles */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2" data-tour="dashboard-stats">
        <MiniStat
          icon={ClipboardDocumentListIcon}
          iconBg="bg-blue-500/20"
          iconColor="text-blue-600"
          label="Active Work Orders"
          value={data?.summary.active_work_orders || 0}
          href="/work-orders"
        />
        <MiniStat
          icon={SignalIcon}
          iconBg="bg-blue-500/20"
          iconColor="text-werco-navy-600"
          label="Signed In Now"
          value={data?.summary.signed_in_users || 0}
          subtitle="Live ERP sessions"
        />
        <MiniStat
          icon={UserGroupIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Checked In Now"
          value={data?.summary.checked_in_users || 0}
          subtitle="Active time entries"
          href="/shop-floor"
        />
        <MiniStat
          icon={CalendarIcon}
          iconBg="bg-amber-500/20"
          iconColor="text-amber-600"
          label="Due Today"
          value={data?.summary.due_today || 0}
          href="/work-orders"
        />
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg={data?.summary.overdue ? "bg-red-500/20" : "bg-fd-green/15"}
          iconColor={data?.summary.overdue ? "text-red-600" : "text-fd-green"}
          label="Overdue"
          value={data?.summary.overdue || 0}
          valueColor={data?.summary.overdue ? "text-red-600" : undefined}
          href="/work-orders"
        />
        <MiniStat
          icon={UsersIcon}
          iconBg={(data?.summary.idle_signed_in_users || 0) > 0 ? "bg-amber-500/20" : "bg-slate-800/50"}
          iconColor={(data?.summary.idle_signed_in_users || 0) > 0 ? "text-amber-600" : "text-slate-400"}
          label="Signed In, Idle"
          value={data?.summary.idle_signed_in_users || 0}
          subtitle="Not clocked into work"
        />
        <MiniStat
          icon={WrenchScrewdriverIcon}
          iconBg={equipmentDue > 0 ? "bg-amber-500/20" : "bg-fd-green/15"}
          iconColor={equipmentDue > 0 ? "text-amber-600" : "text-fd-green"}
          label="Calibration Due"
          value={equipmentDue}
          subtitle="Within 30 days"
          href="/calibration"
        />
        <MiniStat
          icon={CubeIcon}
          iconBg={lowInventory > 0 ? "bg-red-500/20" : "bg-fd-green/15"}
          iconColor={lowInventory > 0 ? "text-red-600" : "text-fd-green"}
          label="Low Stock Items"
          value={lowInventory}
          valueColor={lowInventory > 0 ? "text-red-600" : undefined}
          href="/inventory"
        />
        <MiniStat
          icon={CheckCircleIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Completed Today"
          value={data?.summary.completed_today ?? data?.recent_completions?.length ?? 0}
          href="/work-orders"
        />
        <MiniStat
          icon={ShieldExclamationIcon}
          iconBg={openNCRs > 0 ? "bg-fd-amber/15" : "bg-fd-green/15"}
          iconColor={openNCRs > 0 ? "text-fd-amber" : "text-fd-green"}
          label="Open NCRs"
          value={openNCRs}
          valueColor={openNCRs > 0 ? "text-fd-amber" : undefined}
          href="/quality"
        />
      </div>

      {/* COCKPIT GRID — all four panels co-visible */}
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-12 gap-4 items-start">
        {/* Capacity Overview */}
        <CockpitPanel
          className="xl:col-span-7"
          title="Capacity Overview"
          subtitle="7-day load per machine"
          footer={`${machineCapacityOverview.length} machine${machineCapacityOverview.length === 1 ? '' : 's'}`}
          headerExtra={
            <div className="flex items-center gap-4 text-xs tabular-nums">
              <span className="flex flex-col leading-tight">
                <span className="text-[10px] uppercase tracking-wide text-slate-500">Sched</span>
                <span className="font-bold text-white">{totalScheduledHours.toFixed(1)}h</span>
              </span>
              <span className="flex flex-col leading-tight">
                <span className="text-[10px] uppercase tracking-wide text-slate-500">Cap</span>
                <span className="font-bold text-white">{totalCapacityHours.toFixed(1)}h</span>
              </span>
              <span className="flex flex-col leading-tight">
                <span className="text-[10px] uppercase tracking-wide text-slate-500">Util</span>
                <span className={`font-bold ${totalCapacityUtilization > 100 ? 'text-red-500' : totalCapacityUtilization >= 90 ? 'text-amber-400' : 'text-emerald-400'}`}>
                  {Math.round(totalCapacityUtilization)}%
                </span>
              </span>
              <Link to="/scheduling" className="btn-ghost btn-sm whitespace-nowrap">
                Schedule
              </Link>
            </div>
          }
        >
          {machineCapacityOverview.length > 0 ? (
            <div className="divide-y divide-fd-line">
              {machineCapacityOverview.map((machine) => (
                <MachineRow
                  key={machine.work_center_id}
                  machine={machine}
                  capacityDayClass={capacityDayClass}
                  capacityStatusLabel={capacityStatusLabel}
                />
              ))}
            </div>
          ) : (
            <p className="text-sm text-slate-400">No machine capacity data available.</p>
          )}
        </CockpitPanel>

        {/* Live Shop Activity */}
        <CockpitPanel
          className="xl:col-span-5"
          title="Live Shop Activity"
          subtitle="Active jobs by work center"
          footer={`${activeAssignments.length} active assignment${activeAssignments.length === 1 ? '' : 's'}`}
        >
          {activeAssignments.length > 0 ? (
            <div>
              {Object.entries(assignmentsByWorkCenter).map(([workCenterName, assignments]) => {
                const workCenterId = assignments[0]?.work_center.id;
                return (
                  <div key={workCenterName} id={`wc-live-${workCenterId}`} className="scroll-mt-2">
                    <div className="sticky top-0 z-10 bg-fd-panel border-b border-fd-line py-1.5">
                      <p className="truncate text-xs font-semibold uppercase tracking-wide text-slate-300">
                        {workCenterName} <span className="text-slate-500">· {assignments.length}</span>
                      </p>
                    </div>
                    <div className="divide-y divide-fd-line">
                      {assignments.map((assignment) => (
                        <ActiveAssignmentRow
                          key={assignment.time_entry_id}
                          assignment={assignment}
                          nowMs={nowMs}
                        />
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="py-10 text-center">
              <UserGroupIcon className="mx-auto h-9 w-9 text-slate-500" />
              <p className="mt-3 text-sm font-medium text-slate-300">No one is clocked into a job right now</p>
              <p className="mt-1 text-xs text-slate-400">
                Signed-in users still appear in the presence panel.
              </p>
            </div>
          )}
        </CockpitPanel>

        {/* Work Center Status */}
        <CockpitPanel
          className="xl:col-span-7"
          title="Work Center Status"
          subtitle="Real-time station status"
          footer={`${data?.work_centers.length || 0} work center${(data?.work_centers.length || 0) === 1 ? '' : 's'}`}
          headerExtra={
            <Link to="/work-centers" className="btn-ghost btn-sm">
              View All
            </Link>
          }
        >
          {data?.work_centers.length ? (
            <div className="divide-y divide-fd-line">
              {data.work_centers.map((wc: WorkCenterStatus) => (
                <WorkCenterRow key={wc.id} wc={wc} />
              ))}
            </div>
          ) : (
            <p className="text-sm text-slate-400">No work centers configured.</p>
          )}
        </CockpitPanel>

        {/* Signed In / Presence */}
        <CockpitPanel
          className="xl:col-span-5"
          title="Signed In Right Now"
          subtitle="Live user presence"
          footer={`${signedInUsers.length} signed in`}
          headerExtra={
            <div className="flex items-center gap-1.5 text-[10px] font-semibold tabular-nums">
              <span className="rounded-sm bg-slate-800/60 px-1.5 py-0.5 text-slate-300">
                {data?.summary.signed_in_users || 0} in
              </span>
              <span className="rounded-sm bg-fd-green/15 px-1.5 py-0.5 text-emerald-400">
                {data?.summary.checked_in_users || 0} job
              </span>
              <span className="rounded-sm bg-amber-500/15 px-1.5 py-0.5 text-amber-400">
                {idleSignedInUsers.length} idle
              </span>
            </div>
          }
        >
          {signedInUsers.length ? (
            <div className="space-y-3">
              {onJobSignedInUsers.length > 0 && (
                <div>
                  <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                    On a job ({onJobSignedInUsers.length})
                  </p>
                  <div className="flex flex-wrap gap-1">
                    {onJobSignedInUsers.map((user) => {
                      const timeEntryId = timeEntryByUserId.get(user.id);
                      return (
                        <button
                          key={user.id}
                          type="button"
                          onClick={() => {
                            if (timeEntryId != null) {
                              document
                                .getElementById(`assign-${timeEntryId}`)
                                ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
                            }
                          }}
                          title={`${user.name} · ${user.role.replace('_', ' ')}${user.active_work_orders.length ? ` · ${user.active_work_orders.join(', ')}` : ''}`}
                          className="inline-flex items-center gap-1 rounded-sm border border-fd-line bg-fd-panel px-2 py-0.5 text-xs font-medium text-slate-200 transition-colors hover:border-fd-line-bright hover:text-white"
                        >
                          <span className="h-1.5 w-1.5 rounded-full bg-fd-green" />
                          {user.name}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}

              {idleSignedInUsers.length > 0 && (
                <div className="divide-y divide-fd-line border-t border-fd-line">
                  {idleSignedInUsers.map((user) => (
                    <IdleUserRow key={user.id} user={user} />
                  ))}
                </div>
              )}

              {idleSignedInUsers.length > 0 && (
                <div className="rounded-sm border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-300">
                  <p className="font-semibold">Signed in but not on a job</p>
                  <p className="mt-0.5">{idleSignedInUsers.map((user) => user.name).join(', ')}</p>
                </div>
              )}
            </div>
          ) : (
            <p className="text-sm text-slate-400">No active signed-in sessions detected.</p>
          )}
        </CockpitPanel>
      </div>

      {/* Recent Completions */}
      <div className="card card-compact flex flex-col min-w-0">
        <div className="card-header !pb-2 !mb-2">
          <div>
            <h2 className="card-title">Recent Completions</h2>
            <p className="card-subtitle">Latest completed operations</p>
          </div>
          <Link to="/work-orders" className="btn-ghost btn-sm">
            View All
          </Link>
        </div>

        {data?.recent_completions.length ? (
          <div className="lg:max-h-[clamp(200px,28vh,360px)] lg:overflow-y-auto divide-y divide-fd-line">
            {data.recent_completions.map((completion, index) => (
              <div
                key={index}
                className="flex items-center gap-3 py-2 first:pt-0"
              >
                <CheckCircleSolid className="h-4 w-4 flex-shrink-0 text-fd-green" />
                <div className="min-w-0 flex-1">
                  <span className="font-semibold text-white">{completion.work_order_number || '-'}</span>
                  <span className="ml-2 truncate text-sm text-slate-400">
                    {completion.operation_name || 'Operation completed'}
                    {completion.operator_name ? ` · ${completion.operator_name}` : ''}
                  </span>
                </div>
                <div className="flex flex-shrink-0 items-center gap-3 text-right text-xs tabular-nums">
                  <span className="w-16 text-slate-300">
                    {completion.completed_at ? formatCentralDate(completion.completed_at, { year: undefined }) : '-'}
                  </span>
                  <span className="w-16 text-slate-400">
                    {completion.completed_at ? formatCentralTime(completion.completed_at) : '-'}
                  </span>
                  <span className="w-16 font-semibold text-white">
                    {completion.quantity_complete} units
                  </span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="py-8 text-center">
            <div className="mx-auto mb-3 w-fit rounded-sm bg-slate-800/60 p-3">
              <ClipboardDocumentListIcon className="h-7 w-7 text-slate-500" />
            </div>
            <p className="text-sm text-slate-400">No recent completions</p>
          </div>
        )}
      </div>
    </div>
  );
}

function MachineRow({
  machine,
  capacityDayClass,
  capacityStatusLabel,
}: {
  machine: MachineCapacityOverview;
  capacityDayClass: (day: CapacityHeatmapDay) => string;
  capacityStatusLabel: (day: CapacityHeatmapDay) => string;
}) {
  const utilization = machine.utilization_pct;
  const barClass =
    utilization > 100 ? 'bg-fd-red' :
    utilization >= 90 ? 'bg-fd-amber' :
    utilization >= 70 ? 'bg-fd-amber' : 'bg-fd-green';

  return (
    <div className="py-2 first:pt-0">
      <div className="flex items-center gap-2">
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          <span className="flex-shrink-0 font-semibold text-white">{machine.work_center_code}</span>
          <span className="truncate text-xs text-slate-400">{machine.work_center_name}</span>
          {machine.overloaded_days > 0 && (
            <span
              className="flex-shrink-0 text-red-500"
              title={`${machine.overloaded_days} overloaded day${machine.overloaded_days === 1 ? '' : 's'}`}
              aria-label={`${machine.overloaded_days} overloaded days`}
            >
              ▲
            </span>
          )}
        </div>
        <div className="flex w-28 flex-shrink-0 items-center gap-2">
          <div className="h-1.5 flex-1 rounded-sm bg-slate-800">
            <div className={`h-1.5 rounded-sm ${barClass}`} style={{ width: `${Math.min(100, utilization)}%` }} />
          </div>
          <span className={`w-9 text-right text-xs font-bold tabular-nums ${utilization > 100 ? 'text-red-500' : utilization >= 90 ? 'text-amber-400' : 'text-emerald-400'}`}>
            {Math.round(utilization)}%
          </span>
        </div>
      </div>
      <div className="mt-1.5 grid grid-cols-7 gap-0.5">
        {machine.days.map((day) => {
          const statusLabel = capacityStatusLabel(day);
          return (
            <Link
              to="/scheduling"
              key={`${machine.work_center_id}-${day.date}`}
              className={`h-9 rounded-sm border px-1 text-center transition hover:brightness-110 focus:outline-none focus:ring-2 focus:ring-fd-blue ${capacityDayClass(day)}`}
              title={`${formatCentralDate(day.date, { year: undefined })} - ${statusLabel}: ${day.scheduled_hours.toFixed(1)}h scheduled of ${day.capacity_hours.toFixed(1)}h capacity (${Math.round(day.utilization_pct)}%)`}
              aria-label={`${machine.work_center_code} ${formatCentralDate(day.date, { year: undefined })}: ${statusLabel}, ${day.scheduled_hours.toFixed(1)} scheduled hours of ${day.capacity_hours.toFixed(1)} capacity hours`}
            >
              <span className="block text-[10px] font-semibold leading-4">
                {formatInCentralTime(day.date, { weekday: 'short' })}
              </span>
              <span className="block text-[11px] font-bold leading-4 tabular-nums">
                {Math.round(day.utilization_pct)}%
              </span>
            </Link>
          );
        })}
      </div>
    </div>
  );
}

function ActiveAssignmentRow({ assignment, nowMs }: { assignment: ActiveAssignment; nowMs: number }) {
  const orderedQty = Number(assignment.work_order.quantity_ordered || 0);
  const completeQty = Number(assignment.operation.quantity_complete ?? assignment.work_order.quantity_complete ?? 0);
  const progress = orderedQty > 0 ? Math.min(100, Math.round((completeQty / orderedQty) * 100)) : 0;
  const dueDate = assignment.work_order.due_date;
  const isOverdue = Boolean(dueDate && isDateBeforeTodayInCentral(dueDate));

  // Fields not shown inline are preserved in the row title so no data is lost.
  const rowTitle = [
    getEntryTypeLabel(assignment.entry_type),
    `Started ${formatCentralTime(assignment.clock_in)}`,
    `Due ${dueDate ? formatCentralDate(dueDate, { year: undefined }) : 'none'}`,
    assignment.work_order.customer_name || null,
    assignment.work_order.priority ? `Priority ${assignment.work_order.priority}` : null,
    assignment.work_order.part_name || null,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <div id={`assign-${assignment.time_entry_id}`} className="flex items-center gap-2 px-4 py-1.5 scroll-mt-10" title={rowTitle}>
      {isOverdue && <span className="h-1.5 w-1.5 flex-shrink-0 rounded-full bg-fd-red" aria-label="Overdue" />}
      <div className="flex min-w-0 flex-1 items-center gap-1.5">
        <span className="truncate font-medium text-slate-200">{assignment.user.name}</span>
        <span className="flex-shrink-0 text-[10px] text-slate-500 tabular-nums">{assignment.user.employee_id}</span>
        <span className={`flex-shrink-0 rounded-sm px-1 py-0.5 text-[10px] font-medium ${getRoleBadgeClass(assignment.user.role)}`}>
          {assignment.user.role.replace('_', ' ')}
        </span>
      </div>
      <Link
        to={`/work-orders/${assignment.work_order.id}`}
        className="flex-shrink-0 text-xs font-semibold text-werco-700 hover:text-werco-800"
      >
        {assignment.work_order.work_order_number}
      </Link>
      <span className="hidden w-24 flex-shrink-0 truncate text-[11px] text-slate-400 sm:block">
        {assignment.work_order.part_number}
        {assignment.operation.operation_number ? ` · ${assignment.operation.operation_number}` : ''}
      </span>
      <div className="flex w-16 flex-shrink-0 items-center gap-1">
        <div className="h-1 flex-1 rounded-sm bg-slate-800">
          <div className="h-1 rounded-sm bg-werco-500" style={{ width: `${progress}%` }} />
        </div>
      </div>
      <span className="w-14 flex-shrink-0 text-right text-[10px] text-slate-500 tabular-nums">
        {completeQty}/{orderedQty || 0} ({progress}%)
      </span>
      <span className="w-12 flex-shrink-0 text-right text-xs text-slate-400 tabular-nums">
        {formatElapsed(assignment.clock_in, nowMs)}
      </span>
    </div>
  );
}

function WorkCenterRow({ wc }: { wc: WorkCenterStatus }) {
  const statusStyle = statusColors[wc.status] || statusColors.offline;
  const typeColor = workCenterTypeColors[wc.type] || 'bg-slate-600';

  const scrollToLiveGroup = () => {
    document.getElementById(`wc-live-${wc.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <div className="flex items-center gap-2 py-2 first:pt-0">
      <span className={`h-6 w-1 flex-shrink-0 rounded-sm ${typeColor}`} title={wc.type.replace('_', ' ')} />
      <div
        className={`h-2.5 w-2.5 flex-shrink-0 rounded-full ${statusStyle.dot} animate-pulse`}
        title={wc.status.replace('_', ' ')}
        aria-label={`Status: ${wc.status.replace('_', ' ')}`}
      />
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium text-white">{wc.name}</p>
        <p className="truncate text-[11px] capitalize text-slate-400">{wc.type.replace('_', ' ')}</p>
      </div>
      <div className="flex flex-shrink-0 items-center gap-1 text-[10px] tabular-nums">
        <span className="rounded-sm bg-slate-800/60 px-1.5 py-0.5 text-slate-300" title="Active operations">
          {wc.active_operations} act
        </span>
        <span className="rounded-sm bg-slate-800/60 px-1.5 py-0.5 text-slate-300" title="Queued operations">
          {wc.queued_operations} qd
        </span>
        <button
          type="button"
          onClick={scrollToLiveGroup}
          className="rounded-sm bg-fd-blue/15 px-1.5 py-0.5 font-medium text-fd-blue transition-colors hover:bg-fd-blue/25"
          title="Jump to live activity for this work center"
        >
          {wc.active_people_count} ppl
        </button>
      </div>
    </div>
  );
}

function IdleUserRow({ user }: { user: SignedInUserStatus }) {
  return (
    <div className="flex items-center gap-2 py-1.5">
      <div className="flex min-w-0 flex-1 items-center gap-1.5">
        <span className="truncate font-medium text-slate-200">{user.name}</span>
        <span className="flex-shrink-0 text-[10px] text-slate-500 tabular-nums">{user.employee_id}</span>
        <span className={`flex-shrink-0 rounded-sm px-1 py-0.5 text-[10px] font-medium ${getRoleBadgeClass(user.role)}`}>
          {user.role.replace('_', ' ')}
        </span>
      </div>
      <span className="flex-shrink-0 rounded-sm bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-amber-400">
        Signed In
      </span>
      <span className="w-16 flex-shrink-0 text-right text-[11px] text-slate-400 tabular-nums">
        {user.connected_since ? formatCentralTime(user.connected_since) : 'Unknown'}
      </span>
    </div>
  );
}

