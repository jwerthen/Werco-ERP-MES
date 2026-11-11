/**
 * Visitors On Site — `GET /visitor-logs/?on_site_only=true&limit=10` (ADMIN/MANAGER/
 * SUPERVISOR on the server, so the widget is offered to exactly those roles). Shows who is
 * signed in at the lobby, their company and host, and when they arrived (Central). The
 * visitor's phone number is in the payload and is deliberately never rendered.
 */
import React from 'react';
import { UserGroupIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState } from '../../../ui';
import { formatCentralDateTime, formatCentralTime, isDateTodayInCentral } from '../../../../utils/centralTime';
import type { VisitorLogListResponse } from '../../../../types/visitor';
import { useDashboardSource } from '../../DashboardWidgetContext';
import { CountChip, PanelAction, SourceBody, panelSubtitle } from './shared';

export function VisitorsOnSiteWidget() {
  const state = useDashboardSource<VisitorLogListResponse>('visitors');
  const total = !state.forbidden && state.data ? state.data.total : null;

  return (
    <CockpitPanel
      title="Visitors On Site"
      subtitle={panelSubtitle([state], 'Signed in at the lobby')}
      footer={total != null && state.data && total > state.data.items.length ? `${total} on site` : undefined}
      headerExtra={
        state.forbidden ? undefined : (
          <div className="flex items-center gap-2">
            {total != null && <CountChip variant={total > 0 ? 'blue' : 'slate'}>{total} on site</CountChip>}
            <PanelAction href="/visitor-log" label="Log" ariaLabel="Open visitor log" />
          </div>
        )
      }
    >
      <SourceBody state={state} loadingText="Loading visitors…" errorTitle="Visitor list is unavailable">
        {data =>
          data.items.length === 0 ? (
            <EmptyState
              icon={UserGroupIcon}
              title="No visitors on site"
              description="Visitors signed in at the lobby appear here."
            />
          ) : (
            <ul className="divide-y divide-fd-line">
              {data.items.map(visitor => (
                <li key={visitor.id} className="flex items-center gap-2 py-1.5 first:pt-0">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-semibold text-white">
                      {visitor.visitor_name}
                      {visitor.visitor_company ? (
                        <span className="ml-1 font-normal text-slate-400">· {visitor.visitor_company}</span>
                      ) : null}
                    </p>
                    <p className="truncate text-[11px] text-slate-400">
                      {visitor.host_name ? `Host: ${visitor.host_name}` : 'No host named'}
                    </p>
                  </div>
                  <span className="flex-shrink-0 text-[11px] tabular-nums text-slate-300" title="Signed in">
                    {isDateTodayInCentral(visitor.signed_in_at)
                      ? formatCentralTime(visitor.signed_in_at)
                      : formatCentralDateTime(visitor.signed_in_at, { year: undefined })}
                  </span>
                </li>
              ))}
            </ul>
          )
        }
      </SourceBody>
    </CockpitPanel>
  );
}
