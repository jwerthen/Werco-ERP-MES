/**
 * Notifications — the unread count comes from `useNotificationState`, the shared module store
 * the header bell already polls (subscribing adds no second poller). The five newest unread
 * rows come from the `notifications` source (`GET /notifications?unread=true&page_size=5`),
 * which is refetched when that store's unread count changes (this widget invalidates the
 * list cache and asks the page for a non-forced refresh) or once the list is two minutes old.
 *
 * Read-only: opening a row follows its deep link; marking read stays in the bell and the
 * Notifications page.
 */
import React, { useEffect, useRef } from 'react';
import { BellIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState } from '../../../ui';
import { useNotificationState } from '../../../../hooks/useNotificationState';
import { formatCentralDateTime } from '../../../../utils/centralTime';
import { variantClass, type StatusVariant } from '../../../../utils/statusColors';
import type { NotificationListResponse } from '../../../../types/notification';
import { useDashboardSource, useDashboardWidgetEnv } from '../../DashboardWidgetContext';
import { invalidateNotificationList } from './notificationListCache';
import { CountChip, PanelAction, SourceBody, WidgetLink, humanize, panelSubtitle } from './shared';

const SEVERITY_VARIANT: Record<string, StatusVariant> = {
  critical: 'red',
  warning: 'amber',
  info: 'blue',
};

export function NotificationsWidget() {
  const state = useDashboardSource<NotificationListResponse>('notifications');
  const { refresh } = useDashboardWidgetEnv();
  const { unreadCount, revision } = useNotificationState();
  const previous = useRef({ revision, unreadCount });

  // A real change in the unread count (a new notification, or one read in the bell or on
  // another tab) invalidates the list. The first count after mount or a session reset
  // (null → N) is not a change: the list was fetched at the same moment.
  useEffect(() => {
    const before = previous.current;
    previous.current = { revision, unreadCount };
    if (revision === before.revision || before.unreadCount === null || unreadCount === null) return;
    invalidateNotificationList();
    refresh(false);
  }, [revision, unreadCount, refresh]);

  const listTotal = !state.forbidden && state.data ? state.data.pagination.total_count : null;
  const count = state.forbidden ? null : (unreadCount ?? listTotal);

  return (
    <CockpitPanel
      title="Notifications"
      subtitle={panelSubtitle([state], 'Newest unread')}
      headerExtra={
        state.forbidden ? undefined : (
          <div className="flex items-center gap-2">
            {count != null && <CountChip variant={count > 0 ? 'blue' : 'slate'}>{count} unread</CountChip>}
            <PanelAction href="/notifications?show=unread" label="All" ariaLabel="Open all unread notifications" />
          </div>
        )
      }
    >
      <SourceBody state={state} loadingText="Loading notifications…" errorTitle="Notifications are unavailable">
        {data =>
          data.items.length === 0 || unreadCount === 0 ? (
            <EmptyState
              icon={BellIcon}
              title="You're all caught up"
              description="New notifications appear here and in the bell."
            />
          ) : (
            <ul className="divide-y divide-fd-line">
              {data.items.map(item => {
                const variant = SEVERITY_VARIANT[item.severity] ?? 'slate';
                return (
                  <li key={item.id} className="flex items-start gap-2 py-1.5 first:pt-0">
                    <span
                      className={`mt-0.5 flex-shrink-0 rounded-sm px-1 py-0.5 text-[10px] font-medium ${variantClass[variant]}`}
                    >
                      {humanize(item.severity)}
                    </span>
                    <div className="min-w-0 flex-1">
                      {item.link ? (
                        <WidgetLink
                          href={item.link}
                          className="block truncate text-xs font-semibold text-fd-link hover:text-sky-200"
                          textClassName="block truncate text-xs font-semibold text-slate-200"
                        >
                          {item.title}
                        </WidgetLink>
                      ) : (
                        <p className="truncate text-xs font-semibold text-slate-200">{item.title}</p>
                      )}
                      <p className="text-[11px] tabular-nums text-slate-500">
                        {formatCentralDateTime(item.created_at, { year: undefined })}
                      </p>
                    </div>
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
