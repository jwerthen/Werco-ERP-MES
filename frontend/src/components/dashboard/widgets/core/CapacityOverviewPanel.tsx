import React, { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { WrenchScrewdriverIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState, ErrorState } from '../../../ui';
import { formatCentralDate, formatInCentralTime } from '../../../../utils/centralTime';
import { useDashboardSource, useDashboardWidgetEnv } from '../../DashboardWidgetContext';
import type { CapacityHeatmapDay, CapacityHeatmapResponse, CapacityHeatmapRow } from '../../sources/coreSources';
import { NOT_AVAILABLE_FOR_ROLE } from './shared';

interface MachineCapacityOverview extends CapacityHeatmapRow {
  scheduled_hours: number;
  capacity_hours: number;
  utilization_pct: number;
  overloaded_days: number;
  available_hours: number;
}

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

/** Capacity Overview — 7-day load per machine (`capacity` source). */
export function CapacityOverviewPanel() {
  const capacity = useDashboardSource<CapacityHeatmapResponse>('capacity');
  const { canOpen, refresh } = useDashboardWidgetEnv();
  const capacityHeatmap = capacity.data;
  const canOpenSchedule = canOpen('/scheduling');

  const machineCapacityOverview = useMemo<MachineCapacityOverview[]>(() => {
    return (capacityHeatmap?.work_centers || [])
      .map(row => {
        const scheduledHours = row.days.reduce((sum, day) => sum + day.scheduled_hours, 0);
        const capacityHours = row.days.reduce((sum, day) => sum + day.capacity_hours, 0);
        const utilizationPct = capacityHours > 0 ? (scheduledHours / capacityHours) * 100 : 0;
        return {
          ...row,
          scheduled_hours: scheduledHours,
          capacity_hours: capacityHours,
          utilization_pct: utilizationPct,
          overloaded_days: row.days.filter(day => day.overloaded).length,
          available_hours: Math.max(0, capacityHours - scheduledHours),
        };
      })
      .sort((a, b) => b.utilization_pct - a.utilization_pct);
  }, [capacityHeatmap]);
  const totalCapacityHours = machineCapacityOverview.reduce((sum, machine) => sum + machine.capacity_hours, 0);
  const totalScheduledHours = machineCapacityOverview.reduce((sum, machine) => sum + machine.scheduled_hours, 0);
  const totalCapacityUtilization = totalCapacityHours > 0 ? (totalScheduledHours / totalCapacityHours) * 100 : 0;

  return (
    <CockpitPanel
      title="Capacity Overview"
      subtitle={capacity.error ? '7-day load · last verified data' : '7-day load per machine'}
      footer={`${machineCapacityOverview.length} machine${machineCapacityOverview.length === 1 ? '' : 's'}`}
      headerExtra={
        <div className="flex items-center gap-4 text-xs tabular-nums">
          <span className="flex flex-col leading-tight">
            <span className="text-[10px] uppercase tracking-wide text-slate-500">Sched</span>
            <span className="font-bold text-white">{capacityHeatmap ? `${totalScheduledHours.toFixed(1)}h` : '—'}</span>
          </span>
          <span className="flex flex-col leading-tight">
            <span className="text-[10px] uppercase tracking-wide text-slate-500">Cap</span>
            <span className="font-bold text-white">{capacityHeatmap ? `${totalCapacityHours.toFixed(1)}h` : '—'}</span>
          </span>
          <span className="flex flex-col leading-tight">
            <span className="text-[10px] uppercase tracking-wide text-slate-500">Util</span>
            <span
              className={`font-bold ${totalCapacityUtilization > 100 ? 'text-red-500' : totalCapacityUtilization >= 90 ? 'text-amber-400' : 'text-emerald-400'}`}
            >
              {capacityHeatmap ? `${Math.round(totalCapacityUtilization)}%` : '—'}
            </span>
          </span>
          {canOpenSchedule && (
            <Link to="/scheduling" className="btn-ghost btn-sm whitespace-nowrap">
              Schedule
            </Link>
          )}
        </div>
      }
    >
      {capacity.forbidden ? (
        <p className="text-sm text-slate-400">{NOT_AVAILABLE_FOR_ROLE}</p>
      ) : capacity.error && !capacityHeatmap ? (
        <ErrorState message="Capacity data is unavailable" onRetry={() => refresh(true)} />
      ) : capacity.loading && !capacityHeatmap ? (
        <p role="status">Loading capacity…</p>
      ) : machineCapacityOverview.length > 0 ? (
        <div className="divide-y divide-fd-line">
          {machineCapacityOverview.map(machine => (
            <MachineRow key={machine.work_center_id} machine={machine} linkToSchedule={canOpenSchedule} />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={WrenchScrewdriverIcon}
          title="No machine capacity data"
          description="Scheduled work across your machines will show up here."
        />
      )}
    </CockpitPanel>
  );
}

function MachineRow({ machine, linkToSchedule }: { machine: MachineCapacityOverview; linkToSchedule: boolean }) {
  const utilization = machine.utilization_pct;
  const barClass =
    utilization > 100
      ? 'bg-fd-red'
      : utilization >= 90
        ? 'bg-fd-amber'
        : utilization >= 70
          ? 'bg-fd-amber'
          : 'bg-fd-green';

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
          <span
            className={`w-9 text-right text-xs font-bold tabular-nums ${utilization > 100 ? 'text-red-500' : utilization >= 90 ? 'text-amber-400' : 'text-emerald-400'}`}
          >
            {Math.round(utilization)}%
          </span>
        </div>
      </div>
      <div className="mt-1.5 grid grid-cols-7 gap-0.5">
        {machine.days.map(day => {
          const statusLabel = capacityStatusLabel(day);
          const dayLabel = formatCentralDate(day.date, { year: undefined });
          const title = `${dayLabel} - ${statusLabel}: ${day.scheduled_hours.toFixed(1)}h scheduled of ${day.capacity_hours.toFixed(1)}h capacity (${Math.round(day.utilization_pct)}%)`;
          const label = `${machine.work_center_code} ${dayLabel}: ${statusLabel}, ${day.scheduled_hours.toFixed(1)} scheduled hours of ${day.capacity_hours.toFixed(1)} capacity hours`;
          const cell = (
            <>
              <span className="block text-[10px] font-semibold leading-4">
                {formatInCentralTime(day.date, { weekday: 'short' })}
              </span>
              <span className="block text-[11px] font-bold leading-4 tabular-nums">
                {Math.round(day.utilization_pct)}%
              </span>
            </>
          );
          return linkToSchedule ? (
            <Link
              to="/scheduling"
              key={`${machine.work_center_id}-${day.date}`}
              className={`h-9 rounded-sm border px-1 text-center transition hover:brightness-110 focus:outline-none focus:ring-2 focus:ring-fd-blue ${capacityDayClass(day)}`}
              title={title}
              aria-label={label}
            >
              {cell}
            </Link>
          ) : (
            <div
              key={`${machine.work_center_id}-${day.date}`}
              className={`h-9 rounded-sm border px-1 text-center ${capacityDayClass(day)}`}
              title={title}
            >
              <span className="sr-only">{label}</span>
              <span aria-hidden="true">{cell}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
