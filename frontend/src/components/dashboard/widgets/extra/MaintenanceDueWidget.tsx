/**
 * Maintenance Due — `GET /maintenance/dashboard` (the `maintenance` source, cached five
 * minutes). Renders the three workload counts only. Overdue and "due in 7 days" add the PM
 * schedules that have come due with no work order being worked (`schedules_overdue` /
 * `schedules_due_this_week`) to the work-order counts — a PM nobody has started, or whose work
 * order is on hold, is still due; the server never counts one PM in both, and an older API
 * without those keys just leaves them out. The response's cost fields
 * (`total_cost_month`, per-work-center `total_cost`) are deliberately not shown — the
 * Maintenance page is where cost belongs. (Not rendering them is a presentation choice; the
 * payload still carries them.)
 */
import React from 'react';
import { WrenchScrewdriverIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState } from '../../../ui';
import type { MaintenanceDashboardResponse } from '../../../../types/dashboardWidgetData';
import { useDashboardSource } from '../../DashboardWidgetContext';
import { PanelAction, PanelStat, SourceBody, panelSubtitle } from './shared';

export function MaintenanceDueWidget() {
  const state = useDashboardSource<MaintenanceDashboardResponse>('maintenance');

  return (
    <CockpitPanel
      title="Maintenance Due"
      subtitle={panelSubtitle([state], 'Maintenance work orders and PM schedules')}
      headerExtra={
        state.forbidden ? undefined : (
          <PanelAction href="/maintenance" label="Maintenance" ariaLabel="Open maintenance" />
        )
      }
    >
      <SourceBody state={state} loadingText="Loading maintenance…" errorTitle="Maintenance status is unavailable">
        {dashboard => {
          const overdue = (dashboard.overdue_count ?? 0) + (dashboard.schedules_overdue ?? 0);
          const dueThisWeek = (dashboard.due_this_week ?? 0) + (dashboard.schedules_due_this_week ?? 0);
          const inProgress = dashboard.in_progress ?? 0;
          if (overdue === 0 && dueThisWeek === 0 && inProgress === 0) {
            return (
              <EmptyState
                icon={WrenchScrewdriverIcon}
                title="No maintenance due"
                description="Nothing overdue, due in the next 7 days, or in progress."
              />
            );
          }
          return (
            <div className="grid grid-cols-3 gap-1.5">
              <PanelStat label="Overdue" value={overdue} valueClassName={overdue > 0 ? 'text-red-400' : 'text-white'} />
              <PanelStat
                label="Due in 7 days"
                value={dueThisWeek}
                valueClassName={dueThisWeek > 0 ? 'text-amber-400' : 'text-white'}
              />
              <PanelStat label="In progress" value={inProgress} />
            </div>
          );
        }}
      </SourceBody>
    </CockpitPanel>
  );
}
