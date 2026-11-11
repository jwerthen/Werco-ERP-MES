/**
 * Overdue Work Orders / Work Orders Due Today — `GET /work-orders/browse` with
 * `scope=overdue|due_today`, `hide_cots=false` (so the header count equals the Overdue and
 * Due Today tiles, which count purchased-part work orders too). "Overdue" here is the tile's
 * definition: due date before Central today. Late Jobs uses the PROMISE date instead.
 *
 * Rows show work order, part, due date and status only — no customer or customer PO, the
 * same posture the wallboard keeps for a screen that may sit in a shared office.
 */
import React from 'react';
import { CalendarIcon, CheckCircleIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState, StatusBadge } from '../../../ui';
import { formatCentralDate } from '../../../../utils/centralTime';
import type { WorkOrderBrowseResponse } from '../../../../types/workOrderBrowse';
import type { StatusVariant } from '../../../../utils/statusColors';
import { useDashboardSource } from '../../DashboardWidgetContext';
import { CountChip, PanelAction, SourceBody, WidgetLink, WO_LINK_CLASS, WO_TEXT_CLASS, panelSubtitle } from './shared';

interface WorkOrderListConfig {
  sourceKey: 'workOrdersOverdue' | 'workOrdersDueToday';
  title: string;
  subtitle: string;
  viewAllHref: string;
  viewAllLabel: string;
  countLabel: string;
  countVariant: StatusVariant;
  dueClassName: string;
  loadingText: string;
  errorTitle: string;
  emptyIcon: React.ComponentType<{ className?: string }>;
  emptyTitle: string;
  emptyDescription: string;
}

function WorkOrderListPanel({ config }: { config: WorkOrderListConfig }) {
  const state = useDashboardSource<WorkOrderBrowseResponse>(config.sourceKey);
  const total = state.data?.total ?? null;
  const plural = total === 1 ? '' : 's';

  return (
    <CockpitPanel
      title={config.title}
      subtitle={panelSubtitle([state], config.subtitle)}
      footer={state.data && !state.forbidden ? `${total} work order${plural}` : undefined}
      headerExtra={
        state.forbidden ? undefined : (
          <div className="flex items-center gap-2">
            {total != null && (
              <CountChip variant={total > 0 ? config.countVariant : 'slate'} title={`${total} ${config.countLabel}`}>
                {total} {config.countLabel}
              </CountChip>
            )}
            <PanelAction href={config.viewAllHref} label="View all" ariaLabel={config.viewAllLabel} />
          </div>
        )
      }
    >
      <SourceBody state={state} loadingText={config.loadingText} errorTitle={config.errorTitle}>
        {data =>
          data.items.length === 0 ? (
            <EmptyState icon={config.emptyIcon} title={config.emptyTitle} description={config.emptyDescription} />
          ) : (
            <ul className="divide-y divide-fd-line">
              {data.items.map(wo => (
                <li key={wo.id} className="flex items-center gap-2 py-1.5 first:pt-0">
                  <WidgetLink href={`/work-orders/${wo.id}`} className={WO_LINK_CLASS} textClassName={WO_TEXT_CLASS}>
                    {wo.work_order_number}
                  </WidgetLink>
                  <span
                    className="min-w-0 flex-1 truncate text-[11px] text-slate-400"
                    title={wo.part_name ?? undefined}
                  >
                    {wo.part_number || wo.part_name || '—'}
                  </span>
                  <span className={`w-14 flex-shrink-0 text-right text-[11px] tabular-nums ${config.dueClassName}`}>
                    {wo.due_date ? formatCentralDate(wo.due_date, { year: undefined }) : '—'}
                  </span>
                  <StatusBadge status={wo.status} className="flex-shrink-0 !text-[10px]" />
                </li>
              ))}
            </ul>
          )
        }
      </SourceBody>
    </CockpitPanel>
  );
}

const OVERDUE_CONFIG: WorkOrderListConfig = {
  sourceKey: 'workOrdersOverdue',
  title: 'Overdue Work Orders',
  subtitle: 'Past due date (Central) · oldest due first',
  viewAllHref: '/work-orders?scope=overdue&cots=1',
  viewAllLabel: 'View all overdue work orders',
  countLabel: 'overdue',
  countVariant: 'red',
  dueClassName: 'text-red-400',
  loadingText: 'Loading overdue work orders…',
  errorTitle: 'Overdue work orders are unavailable',
  emptyIcon: CheckCircleIcon,
  emptyTitle: 'No overdue work orders',
  emptyDescription: 'Every open work order is on or ahead of its due date.',
};

const DUE_TODAY_CONFIG: WorkOrderListConfig = {
  sourceKey: 'workOrdersDueToday',
  title: 'Work Orders Due Today',
  subtitle: 'Due today (Central)',
  viewAllHref: '/work-orders?scope=due_today&cots=1',
  viewAllLabel: 'View all work orders due today',
  countLabel: 'due',
  countVariant: 'amber',
  dueClassName: 'text-slate-300',
  loadingText: 'Loading work orders due today…',
  errorTitle: 'Work orders due today are unavailable',
  emptyIcon: CalendarIcon,
  emptyTitle: 'Nothing due today',
  emptyDescription: 'Open work orders due today will appear here.',
};

export function WorkOrdersOverdueWidget() {
  return <WorkOrderListPanel config={OVERDUE_CONFIG} />;
}

export function WorkOrdersDueTodayWidget() {
  return <WorkOrderListPanel config={DUE_TODAY_CONFIG} />;
}
