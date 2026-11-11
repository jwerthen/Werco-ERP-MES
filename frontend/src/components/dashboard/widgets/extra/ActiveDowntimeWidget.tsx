/**
 * Active Downtime — `GET /downtime/active` (open downtime events, newest first).
 *
 * `DowntimeEventResponse` is a plain BaseModel, not UTCModel, so `start_time` can arrive
 * without a `Z`; every timestamp goes through `utils/centralTime` (`toDate` treats a
 * zone-less string as UTC) and renders in Central time.
 *
 * The work-order link is keyed by `work_order_id`, which the server always fills. Its text is
 * `work_order.wo_number` when sent; an API deployed before the `WorkOrderInfo` alias fix sends
 * null there (see `ActiveDowntimeEvent`), so the link then reads "Work order".
 */
import React from 'react';
import { CheckCircleIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState, StatusBadge } from '../../../ui';
import { formatCentralTime, toDate } from '../../../../utils/centralTime';
import { variantClass } from '../../../../utils/statusColors';
import type { ActiveDowntimeEvent } from '../../../../types/dashboardWidgetData';
import { useDashboardSource, useDashboardWidgetEnv } from '../../DashboardWidgetContext';
import { CountChip, PanelAction, SourceBody, WidgetLink, formatDuration, humanize, panelSubtitle } from './shared';

/** Unplanned downtime is the alarm; planned downtime is expected. */
const PLANNED_COLORS: Record<string, string> = {
  unplanned: variantClass.red,
  planned: variantClass.slate,
};

function elapsedMinutes(startTime: string, nowMs: number): number | null {
  const start = toDate(startTime);
  if (!start) return null;
  return Math.max(nowMs - start.getTime(), 0) / 60_000;
}

export function ActiveDowntimeWidget() {
  const state = useDashboardSource<ActiveDowntimeEvent[]>('downtime');
  const { nowMs } = useDashboardWidgetEnv();
  const events = !state.forbidden && state.data ? state.data : null;
  const machines = events ? new Set(events.map(event => event.work_center_id)).size : null;

  return (
    <CockpitPanel
      title="Active Downtime"
      subtitle={panelSubtitle([state], 'Open downtime events · newest first')}
      footer={events ? `${events.length} open event${events.length === 1 ? '' : 's'}` : undefined}
      headerExtra={
        state.forbidden ? undefined : (
          <div className="flex items-center gap-2">
            {machines != null && <CountChip variant={machines > 0 ? 'red' : 'slate'}>{machines} down</CountChip>}
            <PanelAction href="/downtime" label="Downtime" ariaLabel="Open downtime" />
          </div>
        )
      }
    >
      <SourceBody state={state} loadingText="Loading active downtime…" errorTitle="Active downtime is unavailable">
        {rows =>
          rows.length === 0 ? (
            <EmptyState
              icon={CheckCircleIcon}
              title="No machines down"
              description="Open downtime events appear here."
            />
          ) : (
            <ul className="divide-y divide-fd-line">
              {rows.map(event => {
                const minutes = elapsedMinutes(event.start_time, nowMs);
                const workCenter = event.work_center;
                const machineName = workCenter ? workCenter.name : `Work center ${event.work_center_id}`;
                const woNumber = event.work_order?.wo_number || null;
                return (
                  <li key={event.id} className="flex items-center gap-2 py-1.5 first:pt-0">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-xs font-semibold text-white">
                        {machineName}
                        {workCenter?.code ? (
                          <span className="ml-1 font-normal text-slate-400">{workCenter.code}</span>
                        ) : null}
                      </p>
                      <p className="truncate text-[11px] text-slate-400">
                        {humanize(event.category)}
                        {event.reason_code ? ` · ${event.reason_code}` : ''}
                        {event.work_order_id != null ? (
                          <>
                            {' · '}
                            <WidgetLink
                              href={`/work-orders/${event.work_order_id}`}
                              className="text-fd-link hover:text-sky-200"
                              textClassName="text-slate-300"
                              aria-label={woNumber ? undefined : `Open the work order for ${machineName}`}
                            >
                              {woNumber ?? 'Work order'}
                            </WidgetLink>
                          </>
                        ) : null}
                      </p>
                    </div>
                    <StatusBadge
                      status={event.planned_type}
                      className="flex-shrink-0 !text-[10px]"
                      colorMap={PLANNED_COLORS}
                    />
                    <span className="w-16 flex-shrink-0 text-right text-[11px] tabular-nums leading-tight">
                      <span className="block font-semibold text-red-400">
                        {minutes == null ? '—' : formatDuration(minutes)}
                      </span>
                      <span className="block text-slate-400">since {formatCentralTime(event.start_time)}</span>
                    </span>
                  </li>
                );
              })}
            </ul>
          )
        }
      </SourceBody>
    </CockpitPanel>
  );
}
