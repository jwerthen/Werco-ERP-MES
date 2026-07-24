/**
 * NotificationBell — top-bar bell + unread badge + recent-20 popover.
 *
 * PR 1 (in-app inbox) delivery is poll-only: the unread count is fetched on
 * mount and every 60s. Live WebSocket push arrives in PR 2. Clicking the bell
 * opens a keyboard-accessible popover of the 20 most-recent notifications; each
 * row marks itself read (optimistically — rarely rejected) and deep-links to
 * its route. "Mark all read" and "View all" round it out.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { BellIcon, CheckIcon } from '@heroicons/react/24/outline';
import api from '../services/api';
import { NotificationItem } from '../types/notification';
import { EmptyState, StatusBadge, useToast } from './ui';
import { SEVERITY_COLOR_MAP, formatNotificationTime } from '../utils/notificationSeverity';

const POLL_INTERVAL_MS = 60_000;
const RECENT_LIMIT = 20;

export default function NotificationBell() {
  const navigate = useNavigate();
  const { showToast } = useToast();

  const [open, setOpen] = useState(false);
  const [unreadCount, setUnreadCount] = useState(0);
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState(false);

  const wrapperRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  // --- Unread-count poll (mount + every 60s) --------------------------------
  useEffect(() => {
    let cancelled = false;
    const loadCount = async () => {
      try {
        const count = await api.getUnreadCount();
        if (!cancelled) setUnreadCount(count);
      } catch {
        // Transient failure — keep the last known count rather than zeroing it.
      }
    };
    loadCount();
    const interval = setInterval(loadCount, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  const loadRecent = useCallback(async () => {
    setListLoading(true);
    setListError(false);
    try {
      const res = await api.getNotifications({ pageSize: RECENT_LIMIT });
      setItems(res.items);
    } catch {
      setListError(true);
    } finally {
      setListLoading(false);
    }
  }, []);

  // Fetch the recent list each time the popover opens.
  useEffect(() => {
    if (open) loadRecent();
  }, [open, loadRecent]);

  // Close on outside click + Escape while open.
  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (!wrapperRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [open]);

  const markRead = useCallback(
    async (item: NotificationItem) => {
      if (item.is_read) return;
      // Optimistic: flip read + decrement the badge before the server responds.
      setItems((current) => current.map((n) => (n.id === item.id ? { ...n, is_read: true } : n)));
      setUnreadCount((c) => Math.max(0, c - 1));
      try {
        await api.markNotificationRead(item.id);
      } catch (err: any) {
        // Roll back the optimistic change; never a success toast for a failed call.
        setItems((current) => current.map((n) => (n.id === item.id ? { ...n, is_read: false } : n)));
        setUnreadCount((c) => c + 1);
        showToast('error', err?.response?.data?.detail || 'Could not mark the notification read.');
      }
    },
    [showToast]
  );

  const handleRowActivate = (item: NotificationItem) => {
    void markRead(item);
    setOpen(false);
    if (item.link) navigate(item.link);
  };

  const markAllRead = useCallback(async () => {
    const snapshot = items;
    const prevCount = unreadCount;
    // Optimistic: clear all unread locally, then reconcile with the server.
    setItems((current) => current.map((n) => ({ ...n, is_read: true })));
    setUnreadCount(0);
    try {
      await api.markAllNotificationsRead();
    } catch (err: any) {
      setItems(snapshot);
      setUnreadCount(prevCount);
      showToast('error', err?.response?.data?.detail || 'Could not mark all notifications read.');
    }
  }, [items, unreadCount, showToast]);

  const hasUnread = unreadCount > 0;
  const badgeLabel = unreadCount > 99 ? '99+' : String(unreadCount);

  return (
    <div ref={wrapperRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="relative flex items-center justify-center h-[34px] w-[34px] rounded-[3px] text-fd-mute hover:text-fd-body transition-all duration-150"
        style={{ background: 'var(--fd-sunken)', border: '1px solid var(--fd-line)' }}
        aria-label={hasUnread ? `Notifications, ${unreadCount} unread` : 'Notifications'}
        aria-haspopup="true"
        aria-expanded={open}
        title="Notifications"
      >
        <BellIcon className="h-4 w-4" aria-hidden="true" />
        {hasUnread && (
          <span
            className="absolute -top-1.5 -right-1.5 min-w-[16px] h-4 px-1 flex items-center justify-center bg-fd-red text-white text-[10px] font-bold font-mono rounded-full leading-none"
            aria-hidden="true"
          >
            {badgeLabel}
          </span>
        )}
      </button>

      {open && (
        <div
          className="absolute right-0 mt-2 w-[360px] max-w-[calc(100vw-1.5rem)] rounded-[4px] shadow-2xl shadow-black/40 z-[60] overflow-hidden"
          style={{ background: 'var(--fd-panel)', border: '1px solid var(--fd-line-bright)' }}
          role="dialog"
          aria-label="Notifications"
        >
          {/* Header */}
          <div
            className="flex items-center justify-between px-3 py-2.5"
            style={{ borderBottom: '1px solid var(--fd-line)' }}
          >
            <span className="font-mono text-[11px] uppercase tracking-[0.12em] text-fd-body">
              Notifications
            </span>
            <button
              type="button"
              onClick={markAllRead}
              disabled={!hasUnread}
              className="inline-flex items-center gap-1 text-[11px] font-medium text-fd-blue hover:text-fd-ink disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <CheckIcon className="h-3.5 w-3.5" aria-hidden="true" />
              Mark all read
            </button>
          </div>

          {/* Body */}
          <div className="max-h-[380px] overflow-y-auto">
            {listLoading ? (
              <div className="px-3 py-8 text-center text-xs text-fd-mute">Loading…</div>
            ) : listError ? (
              <div className="px-3 py-6 text-center">
                <p className="text-xs text-fd-mute">Couldn't load notifications.</p>
                <button
                  type="button"
                  onClick={loadRecent}
                  className="mt-2 text-[11px] font-medium text-fd-blue hover:text-fd-ink transition-colors"
                >
                  Retry
                </button>
              </div>
            ) : items.length === 0 ? (
              <div className="p-3">
                <EmptyState
                  icon={BellIcon}
                  title="You're all caught up"
                  description="New notifications will appear here."
                />
              </div>
            ) : (
              <ul className="divide-y divide-fd-line">
                {items.map((item) => {
                  const rowClass = `flex w-full items-start gap-2.5 px-3 py-2.5 text-left transition-colors hover:bg-white/[0.03] ${
                    item.is_read ? '' : 'bg-fd-blue/[0.06]'
                  }`;
                  const inner = (
                    <>
                      <span
                        className={`mt-1.5 h-1.5 w-1.5 flex-shrink-0 rounded-full ${
                          item.is_read ? 'bg-transparent' : 'bg-fd-blue'
                        }`}
                        aria-hidden="true"
                      />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2">
                          <StatusBadge status={item.severity} colorMap={SEVERITY_COLOR_MAP} />
                          <span className="text-[10px] font-mono text-fd-faint whitespace-nowrap">
                            {formatNotificationTime(item.created_at)}
                          </span>
                        </span>
                        <span
                          className={`mt-1 block truncate text-[13px] ${
                            item.is_read ? 'text-fd-body' : 'font-semibold text-fd-ink'
                          }`}
                        >
                          {item.title}
                        </span>
                        {item.body && (
                          <span className="mt-0.5 block truncate text-[11px] text-fd-mute">{item.body}</span>
                        )}
                      </span>
                    </>
                  );
                  return (
                    <li key={item.id}>
                      {item.link ? (
                        <Link to={item.link} onClick={() => handleRowActivate(item)} className={rowClass}>
                          {inner}
                        </Link>
                      ) : (
                        <button type="button" onClick={() => handleRowActivate(item)} className={rowClass}>
                          {inner}
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          {/* Footer */}
          <div className="px-3 py-2" style={{ borderTop: '1px solid var(--fd-line)' }}>
            <Link
              to="/notifications"
              onClick={() => setOpen(false)}
              className="block text-center text-[12px] font-medium text-fd-blue hover:text-fd-ink transition-colors"
            >
              View all
            </Link>
          </div>
        </div>
      )}
    </div>
  );
}
