/**
 * Action Inbox digest — `GET /operations-inbox/` (the `actionInbox` source, cached two
 * minutes because each call reads up to seven sources and every active user). The server
 * already filters each source by the viewer's effective permissions.
 *
 * Counts agree with the Action Inbox page's default view: snoozed items (`snoozed_until`
 * set) are excluded, acknowledged items stay. When any source hit the server's per-source
 * cap (`truncated_sources`), counts read "N+" because the real number is larger.
 */
import React from 'react';
import { InboxIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState } from '../../../ui';
import type { OperationalInboxItem, OperationalInboxResponse } from '../../../../types/operationsInbox';
import { useDashboardSource } from '../../DashboardWidgetContext';
import { CountChip, PanelAction, SourceBody, WidgetLink, panelSubtitle } from './shared';

const TOP_HIGH_LIMIT = 5;

/** Severity counts; a non-zero high/medium count takes its alarm color. */
const SEVERITIES: { key: OperationalInboxItem['severity']; label: string; activeClass: string }[] = [
  { key: 'high', label: 'High', activeClass: 'text-red-400' },
  { key: 'medium', label: 'Medium', activeClass: 'text-amber-400' },
  { key: 'low', label: 'Low', activeClass: 'text-white' },
];

export function ActionInboxWidget() {
  const state = useDashboardSource<OperationalInboxResponse>('actionInbox');
  const data = state.forbidden ? null : state.data;
  const active = data ? data.items.filter(item => !item.snoozed_until) : [];
  const truncated = Boolean(data && data.truncated_sources.length > 0);
  const suffix = truncated ? '+' : '';

  return (
    <CockpitPanel
      title="Action Inbox"
      subtitle={panelSubtitle([state], 'Open issues that need an owner or a next step')}
      headerExtra={
        state.forbidden ? undefined : (
          <div className="flex items-center gap-2">
            {data && (
              <CountChip
                variant={active.length > 0 ? 'amber' : 'slate'}
                title={truncated ? 'At least this many — some sources were truncated' : undefined}
              >
                {active.length}
                {suffix} open
              </CountChip>
            )}
            <PanelAction href="/action-inbox" label="Inbox" ariaLabel="Open action inbox" />
          </div>
        )
      }
    >
      <SourceBody state={state} loadingText="Loading action inbox…" errorTitle="Action inbox is unavailable">
        {() => {
          if (active.length === 0) {
            return (
              <EmptyState
                icon={InboxIcon}
                title="Nothing needs action"
                description="Late work, blockers, low stock, quality and purchasing follow-ups appear here."
              />
            );
          }
          const high = active.filter(item => item.severity === 'high');
          return (
            <div className="space-y-2.5">
              <dl className="grid grid-cols-3 gap-1.5">
                {SEVERITIES.map(severity => {
                  const total = active.filter(item => item.severity === severity.key).length;
                  return (
                    <div key={severity.key} className="rounded-sm border border-fd-line px-2 py-1.5">
                      <dt className="text-[10px] font-medium uppercase tracking-wide text-slate-500">
                        {severity.label}
                      </dt>
                      <dd
                        className={`text-lg font-bold leading-tight tabular-nums ${total > 0 ? severity.activeClass : 'text-white'}`}
                      >
                        {total}
                        {total > 0 ? suffix : ''}
                      </dd>
                    </div>
                  );
                })}
              </dl>
              {high.length === 0 ? (
                <p className="text-xs text-slate-400">No high-severity issues.</p>
              ) : (
                <div>
                  <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500">High severity</p>
                  <ul className="divide-y divide-fd-line">
                    {high.slice(0, TOP_HIGH_LIMIT).map(item => (
                      <li key={item.key} className="py-1.5 first:pt-0">
                        <WidgetLink
                          href={item.href}
                          className="block truncate text-xs font-semibold text-fd-link hover:text-sky-200"
                          textClassName="block truncate text-xs font-semibold text-slate-200"
                        >
                          {item.title}
                        </WidgetLink>
                        <p className="truncate text-[11px] text-slate-400">
                          {item.detail}
                          {item.owner_name ? ` · ${item.owner_name}` : ' · Unassigned'}
                          {item.acknowledged ? ' · Acknowledged' : ''}
                        </p>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          );
        }}
      </SourceBody>
    </CockpitPanel>
  );
}
