/**
 * KPI-zone core widgets: one MiniStat each, rendered as a DIRECT child of the page's
 * KPI strip (`data-tour="dashboard-stats"`), so no wrapper element here.
 *
 * Links go through `canOpen` (the PrivateRoute predicate): a tile whose target the
 * viewer cannot open renders static. In-page cross-links degrade the same way when
 * their target widget is hidden.
 */
import React from 'react';
import {
  CalendarIcon,
  CheckCircleIcon,
  ClipboardDocumentCheckIcon,
  ClipboardDocumentListIcon,
  CubeIcon,
  DocumentMagnifyingGlassIcon,
  ExclamationTriangleIcon,
  ShieldExclamationIcon,
  SignalIcon,
  UserGroupIcon,
  UsersIcon,
  WrenchScrewdriverIcon,
} from '@heroicons/react/24/outline';
import { MiniStat } from '../../../cockpit';
import { useDashboardSource, useDashboardWidgetEnv } from '../../DashboardWidgetContext';
import type { EquipmentDueSoonRow, LowStockRow, QualitySummaryData } from '../../sources/coreSources';
import type { SourceState } from '../../widgetTypes';
import { useCoreDashboard } from './CoreDashboardContext';
import { focusIdleOperators, NOT_AVAILABLE_FOR_ROLE } from './shared';

function useLinkIfAllowed() {
  const { canOpen } = useDashboardWidgetEnv();
  return (href: string) => (canOpen(href) ? href : undefined);
}

/** The value a source-backed tile shows: the count, else 'Loading…' / 'Unavailable'. */
function sourceValue<T>(
  state: SourceState<T>,
  count: (data: T) => number
): { count: number | null; value: React.ReactNode } {
  const value = state.data != null ? count(state.data) : null;
  return { count: value, value: value ?? (state.loading ? 'Loading…' : 'Unavailable') };
}

function sourceSubtitle(state: SourceState, fallback?: string): string | undefined {
  if (state.forbidden) return NOT_AVAILABLE_FOR_ROLE;
  if (state.error) return 'Last verified · refresh failed';
  return fallback;
}

export function OverdueTile() {
  const { data } = useCoreDashboard();
  const linkIfAllowed = useLinkIfAllowed();
  return (
    <MiniStat
      icon={ExclamationTriangleIcon}
      iconBg={data?.summary.overdue ? 'bg-red-500/20' : 'bg-fd-green/15'}
      iconColor={data?.summary.overdue ? 'text-red-600' : 'text-fd-green'}
      label="Overdue"
      value={data?.summary.overdue || 0}
      valueColor={data?.summary.overdue ? 'text-red-300' : undefined}
      href={linkIfAllowed('/work-orders?scope=overdue&cots=1')}
    />
  );
}

export function IdleOperatorsTile() {
  const { data } = useCoreDashboard();
  const { isVisible } = useDashboardWidgetEnv();
  return (
    <MiniStat
      icon={UsersIcon}
      iconBg={(data?.summary.idle_signed_in_users || 0) > 0 ? 'bg-amber-500/20' : 'bg-slate-800/50'}
      iconColor={(data?.summary.idle_signed_in_users || 0) > 0 ? 'text-amber-600' : 'text-slate-400'}
      label="Signed In, Idle"
      value={data?.summary.idle_signed_in_users || 0}
      subtitle="Not clocked into work"
      valueColor={(data?.summary.idle_signed_in_users || 0) > 0 ? 'text-amber-300' : undefined}
      onClick={isVisible('presence') ? focusIdleOperators : undefined}
    />
  );
}

export function ActiveWorkOrdersTile() {
  const { data } = useCoreDashboard();
  const linkIfAllowed = useLinkIfAllowed();
  return (
    <MiniStat
      icon={ClipboardDocumentListIcon}
      iconBg="bg-blue-500/20"
      iconColor="text-blue-600"
      label="Active Work Orders"
      subtitle="In progress"
      value={data?.summary.active_work_orders || 0}
      href={linkIfAllowed('/work-orders?status=in_progress&cots=1')}
    />
  );
}

export function SignedInTile() {
  const { data } = useCoreDashboard();
  return (
    <MiniStat
      icon={SignalIcon}
      iconBg="bg-blue-500/20"
      iconColor="text-werco-navy-600"
      label="Signed In Now"
      value={data?.summary.signed_in_users || 0}
      subtitle="Live ERP sessions"
    />
  );
}

export function CheckedInTile() {
  const { data } = useCoreDashboard();
  const linkIfAllowed = useLinkIfAllowed();
  return (
    <MiniStat
      icon={UserGroupIcon}
      iconBg="bg-fd-green/15"
      iconColor="text-fd-green"
      label="Checked In Now"
      value={data?.summary.checked_in_users || 0}
      subtitle="Active time entries"
      href={linkIfAllowed('/shop-floor')}
    />
  );
}

export function DueTodayTile() {
  const { data } = useCoreDashboard();
  const linkIfAllowed = useLinkIfAllowed();
  return (
    <MiniStat
      icon={CalendarIcon}
      iconBg="bg-amber-500/20"
      iconColor="text-amber-600"
      label="Due Today"
      value={data?.summary.due_today || 0}
      href={linkIfAllowed('/work-orders?scope=due_today&cots=1')}
    />
  );
}

export function CalibrationDueTile() {
  const equipment = useDashboardSource<EquipmentDueSoonRow[]>('equipment');
  const linkIfAllowed = useLinkIfAllowed();
  const { count, value } = sourceValue(equipment, rows => rows.length);
  return (
    <MiniStat
      icon={WrenchScrewdriverIcon}
      iconBg={(count ?? 0) > 0 ? 'bg-amber-500/20' : 'bg-fd-green/15'}
      iconColor={(count ?? 0) > 0 ? 'text-amber-600' : 'text-fd-green'}
      label="Calibration Due"
      value={value}
      subtitle={sourceSubtitle(equipment, 'Within 30 days')}
      href={linkIfAllowed('/calibration?filter=due_soon')}
    />
  );
}

export function LowStockTile() {
  const stock = useDashboardSource<LowStockRow[]>('stock');
  const linkIfAllowed = useLinkIfAllowed();
  const { count, value } = sourceValue(stock, rows => rows.length);
  return (
    <MiniStat
      icon={CubeIcon}
      iconBg={(count ?? 0) > 0 ? 'bg-red-500/20' : 'bg-fd-green/15'}
      iconColor={(count ?? 0) > 0 ? 'text-red-600' : 'text-fd-green'}
      label="Low Stock Items"
      subtitle={sourceSubtitle(stock)}
      value={value}
      valueColor={(count ?? 0) > 0 ? 'text-red-600' : undefined}
      href={linkIfAllowed('/warehouse?tab=inventory&filter=low_stock')}
    />
  );
}

export function CompletedTodayTile() {
  const { data } = useCoreDashboard();
  const { isVisible } = useDashboardWidgetEnv();
  return (
    <MiniStat
      icon={CheckCircleIcon}
      iconBg="bg-fd-green/15"
      iconColor="text-fd-green"
      label="Operations Completed Today"
      value={data?.summary.completed_today ?? data?.recent_completions?.length ?? 0}
      onClick={
        isVisible('recent_completions')
          ? () => document.getElementById('recent-completions')?.scrollIntoView({ behavior: 'smooth' })
          : undefined
      }
    />
  );
}

export function OpenNcrsTile() {
  const quality = useDashboardSource<QualitySummaryData>('quality');
  const linkIfAllowed = useLinkIfAllowed();
  const { count, value } = sourceValue(quality, summary => summary.open_ncrs ?? 0);
  return (
    <MiniStat
      icon={ShieldExclamationIcon}
      iconBg={(count ?? 0) > 0 ? 'bg-fd-amber/15' : 'bg-fd-green/15'}
      iconColor={(count ?? 0) > 0 ? 'text-fd-amber' : 'text-fd-green'}
      label="Open NCRs"
      subtitle={sourceSubtitle(quality)}
      value={value}
      valueColor={(count ?? 0) > 0 ? 'text-fd-amber' : undefined}
      href={linkIfAllowed('/quality?tab=ncr&filter=open')}
    />
  );
}

export function OpenCarsTile() {
  const quality = useDashboardSource<QualitySummaryData>('quality');
  const linkIfAllowed = useLinkIfAllowed();
  const { count, value } = sourceValue(quality, summary => summary.open_cars ?? 0);
  return (
    <MiniStat
      icon={ClipboardDocumentCheckIcon}
      iconBg={(count ?? 0) > 0 ? 'bg-fd-amber/15' : 'bg-fd-green/15'}
      iconColor={(count ?? 0) > 0 ? 'text-fd-amber' : 'text-fd-green'}
      label="Open CARs"
      subtitle={sourceSubtitle(quality, 'Corrective actions')}
      value={value}
      valueColor={(count ?? 0) > 0 ? 'text-fd-amber' : undefined}
      href={linkIfAllowed('/quality?tab=car')}
    />
  );
}

export function PendingFaiTile() {
  const quality = useDashboardSource<QualitySummaryData>('quality');
  const linkIfAllowed = useLinkIfAllowed();
  const { count, value } = sourceValue(quality, summary => summary.pending_fais ?? 0);
  return (
    <MiniStat
      icon={DocumentMagnifyingGlassIcon}
      iconBg={(count ?? 0) > 0 ? 'bg-fd-amber/15' : 'bg-fd-green/15'}
      iconColor={(count ?? 0) > 0 ? 'text-fd-amber' : 'text-fd-green'}
      label="Pending FAIs"
      subtitle={sourceSubtitle(quality, 'First article inspections')}
      value={value}
      valueColor={(count ?? 0) > 0 ? 'text-fd-amber' : undefined}
      href={linkIfAllowed('/quality?tab=fai')}
    />
  );
}
