/**
 * Notifications — the full in-app inbox (bell popover's "View all" target).
 *
 * Server-paginated DataTable (the endpoint is offset/limit paged) with
 * unread / category / severity filters, a per-row mark-read action, mark-all-read,
 * and deep links into each notification's route. Timestamps render in Central time.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { BellIcon, CheckIcon, Cog6ToothIcon } from '@heroicons/react/24/outline';
import api from '../services/api';
import BackgroundEmailActivity from '../components/BackgroundEmailActivity';
import { useNotificationState } from '../hooks/useNotificationState';
import { DataTable, DataTableColumn, StatusBadge, Button, useToast } from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { formatCentralDateTime } from '../utils/centralTime';
import { SEVERITY_COLOR_MAP } from '../utils/notificationSeverity';
import {
  NotificationCatalogEntry,
  NotificationItem,
  NotificationListParams,
  PaginationMeta,
} from '../types/notification';

const PAGE_SIZE = 25;

type UnreadFilter = 'all' | 'unread' | 'read';

const SEVERITY_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: 'All severities' },
  { value: 'critical', label: 'Critical' },
  { value: 'warning', label: 'Warning' },
  { value: 'info', label: 'Info' },
];

const formatTimestamp = (ts: string) =>
  formatCentralDateTime(ts, {
    month: '2-digit',
    day: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });

export default function Notifications() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { showToast } = useToast();
  const notificationState = useNotificationState();
  const request = useRef(0);

  const [items, setItems] = useState<NotificationItem[]>([]);
  const [meta, setMeta] = useState<PaginationMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  const requestedShow = searchParams.get('show');
  const requestedUnread = searchParams.get('unread');
  const requestedFilter: UnreadFilter | null = requestedShow === 'all' || requestedShow === 'read' || requestedShow === 'unread'
    ? requestedShow : requestedUnread === 'true' ? 'unread' : requestedUnread === 'false' ? 'read' : null;
  const defaultFilterSettled = useRef(requestedFilter !== null);
  const [page, setPage] = useState(() => {
    const requestedPage = Number(searchParams.get('page'));
    return Number.isInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1;
  });
  const [unreadFilter, setUnreadFilter] = useState<UnreadFilter>(requestedFilter ?? 'unread');
  const [categoryFilter, setCategoryFilter] = useState(searchParams.get('category') ?? '');
  const [severityFilter, setSeverityFilter] = useState(searchParams.get('severity') ?? '');

  useEffect(() => {
    if (!defaultFilterSettled.current && notificationState.unreadCount !== null) {
      defaultFilterSettled.current = true;
      setUnreadFilter(notificationState.unreadCount > 0 ? 'unread' : 'all');
    }
  }, [notificationState.unreadCount]);

  // Catalog drives the category filter options and the per-row Category column.
  const [catalog, setCatalog] = useState<NotificationCatalogEntry[]>([]);

  useEffect(() => {
    let cancelled = false;
    api
      .getNotificationCatalog()
      .then(entries => {
        if (!cancelled) setCatalog(entries);
      })
      .catch(() => {
        // Non-fatal: the category filter simply stays empty if the catalog fails.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const categoryByKey = useMemo(() => {
    const map = new Map<string, string>();
    catalog.forEach(entry => map.set(entry.event_key, entry.category));
    return map;
  }, [catalog]);

  const categoryOptions = useMemo(() => {
    const seen = new Set<string>();
    catalog.forEach(entry => seen.add(entry.category));
    return Array.from(seen).sort((a, b) => a.localeCompare(b));
  }, [catalog]);

  const load = useCallback(async () => {
    const seq = ++request.current;
    setLoading(true);
    setLoadError(false);
    try {
      const params: NotificationListParams = { page, pageSize: PAGE_SIZE };
      if (unreadFilter === 'unread') params.unread = true;
      else if (unreadFilter === 'read') params.unread = false;
      if (categoryFilter) params.category = categoryFilter;
      if (severityFilter) params.severity = severityFilter;

      const res = await api.getNotifications(params);
      if (seq !== request.current) return;
      setItems(res.items);
      setMeta(res.pagination);
    } catch (err) {
      console.error('Failed to load notifications:', err);
      if (seq === request.current) setLoadError(true);
    } finally {
      if (seq === request.current) setLoading(false);
    }
  }, [page, unreadFilter, categoryFilter, severityFilter]);

  useEffect(() => {
    load();
    return () => {
      ++request.current;
    };
  }, [load, notificationState.revision]);

  // Changing a filter always returns to the first (newest) page.
  const changeUnread = (value: UnreadFilter) => {
    defaultFilterSettled.current = true;
    setUnreadFilter(value);
    setPage(1);
  };
  const changeCategory = (value: string) => {
    setCategoryFilter(value);
    setPage(1);
  };
  const changeSeverity = (value: string) => {
    setSeverityFilter(value);
    setPage(1);
  };

  const markRead = useCallback(
    async (item: NotificationItem) => {
      if (item.is_read) return;
      // Optimistic flip — mark-read is UI state and effectively never rejected.
      setItems(current => current.map(n => (n.id === item.id ? { ...n, is_read: true } : n)));
      try {
        await notificationState.markRead(item.id);
      } catch (err: any) {
        setItems(current => current.map(n => (n.id === item.id ? { ...n, is_read: false } : n)));
        showToast('error', err?.response?.data?.detail || 'Could not mark the notification read.');
      }
    },
    [showToast]
  );

  const handleRowClick = (item: NotificationItem) => {
    void markRead(item);
    if (item.link) navigate(item.link);
  };

  const markAllRead = useCallback(async () => {
    try {
      const res = await notificationState.markAllRead();
      showToast(
        'success',
        res.updated > 0
          ? `Marked ${res.updated} notification${res.updated === 1 ? '' : 's'} read.`
          : 'No unread notifications.'
      );
      load();
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'Could not mark all notifications read.');
    }
  }, [showToast, load]);

  const unreadOnPage = useMemo(() => items.filter(n => !n.is_read).length, [items]);

  const columns = useMemo<Array<DataTableColumn<NotificationItem>>>(
    () => [
      {
        key: 'severity',
        header: 'Severity',
        className: 'whitespace-nowrap',
        accessor: n => n.severity,
        render: n => <StatusBadge status={n.severity} colorMap={SEVERITY_COLOR_MAP} />,
      },
      {
        key: 'title',
        header: 'Notification',
        accessor: n => n.title,
        render: n => (
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              {!n.is_read && <span className="h-1.5 w-1.5 flex-shrink-0 rounded-full bg-fd-blue" aria-hidden="true" />}
              <span className={`critical-text ${n.is_read ? 'text-fd-body' : 'font-semibold text-fd-ink'}`}>{n.title}</span>
            </div>
            {n.body && <div className="mt-0.5 text-xs text-fd-mute critical-text max-w-md">{n.body}</div>}
          </div>
        ),
      },
      {
        key: 'category',
        header: 'Category',
        className: 'whitespace-nowrap text-fd-mute',
        accessor: n => categoryByKey.get(n.event_key) || '',
        render: n => categoryByKey.get(n.event_key) || '—',
      },
      {
        key: 'created_at',
        header: 'Received',
        className: 'whitespace-nowrap text-fd-mute',
        accessor: n => n.created_at,
        render: n => formatTimestamp(n.created_at),
        csv: n => formatTimestamp(n.created_at),
      },
      {
        key: 'status',
        header: 'Status',
        align: 'center',
        accessor: n => (n.is_read ? 'Read' : 'Unread'),
        render: n =>
          n.is_read ? (
            <span className="text-xs text-fd-mute">Read</span>
          ) : (
            <span className="inline-flex items-center rounded px-2 py-0.5 text-xs font-medium bg-amber-500/20 text-amber-300">
              Unread
            </span>
          ),
      },
      {
        key: 'actions',
        header: '',
        align: 'right',
        render: n =>
          n.is_read ? null : (
            <button
              type="button"
              onClick={e => {
                e.stopPropagation();
                void markRead(n);
              }}
              className="inline-flex items-center gap-1 text-xs font-medium text-fd-blue hover:text-fd-ink transition-colors"
            >
              <CheckIcon className="h-3.5 w-3.5" aria-hidden="true" />
              Mark read
            </button>
          ),
      },
    ],
    [categoryByKey, markRead]
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center">
          <BellIcon className="h-8 w-8 text-werco-primary mr-3" />
          <div>
            <h1 className="text-2xl font-bold text-white">Notifications</h1>
            <p className="text-sm text-slate-400">Your in-app notification inbox</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Link to="/settings">
            <Button variant="secondary" size="sm">
              <Cog6ToothIcon className="h-4 w-4 mr-1.5" aria-hidden="true" />
              Notification settings
            </Button>
          </Link>
          <Button
            variant="secondary"
            size="sm"
            onClick={markAllRead}
            title="Mark all notifications read across every page and filter"
            disabled={notificationState.unreadCount === 0 || notificationState.pending}
          >
            <CheckIcon className="h-4 w-4 mr-1.5" aria-hidden="true" />
            Mark all read
          </Button>
        </div>
      </div>

      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-3 gap-2">
        <MiniStat
          icon={BellIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Total (this page)"
          value={items.length}
        />
        <MiniStat
          icon={CheckIcon}
          iconBg={unreadOnPage > 0 ? 'bg-fd-amber/15' : 'bg-fd-green/15'}
          iconColor={unreadOnPage > 0 ? 'text-fd-amber' : 'text-fd-green'}
          label="Unread (this page)"
          value={unreadOnPage}
          valueColor={unreadOnPage > 0 ? 'text-fd-amber' : 'text-fd-green'}
        />
        <MiniStat
          icon={BellIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label={unreadFilter === 'all' && !categoryFilter && !severityFilter ? 'Total notifications' : 'Matching notifications'}
          value={meta?.total_count ?? 0}
        />
      </MiniStatStrip>

      {/* Filters */}
      <div className="rounded-sm border border-fd-line bg-fd-panel p-3">
        <div className="flex flex-wrap gap-3 items-end">
          <div>
            <label htmlFor="notif-unread" className="label">
              Show
            </label>
            <select
              id="notif-unread"
              value={unreadFilter}
              onChange={e => changeUnread(e.target.value as UnreadFilter)}
              className="input"
            >
              <option value="all">All</option>
              <option value="unread">Unread only</option>
              <option value="read">Read only</option>
            </select>
          </div>
          <div>
            <label htmlFor="notif-category" className="label">
              Category
            </label>
            <select
              id="notif-category"
              value={categoryFilter}
              onChange={e => changeCategory(e.target.value)}
              className="input"
            >
              <option value="">All categories</option>
              {categoryOptions.map(c => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="notif-severity" className="label">
              Severity
            </label>
            <select
              id="notif-severity"
              value={severityFilter}
              onChange={e => changeSeverity(e.target.value)}
              className="input"
            >
              {SEVERITY_OPTIONS.map(o => (
                <option key={o.value || 'all'} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Server-paged inbox (offset/limit), desc(created_at). */}
      <DataTable<NotificationItem>
        columns={columns}
        data={items}
        rowKey={n => n.id}
        onRowClick={handleRowClick}
        loading={loading}
        error={loadError ? 'Could not load notifications.' : false}
        onRetry={load}
        manualSorting
        csvExport={{ filename: 'notifications', label: 'Export page' }}
        empty={{
          icon: BellIcon,
          title: 'No notifications',
          description: 'Notifications about holds, completions, receipts, quality events, and more will appear here.',
        }}
      />
      {meta && meta.total_count > 0 && (
        <nav aria-label="Notification inbox pagination" className="flex flex-wrap items-center justify-between gap-3 text-sm text-slate-300">
          <p aria-live="polite">
            Page {page} of {Math.max(1, meta.total_pages)} · {meta.total_count} {unreadFilter === 'unread' ? 'unread' : 'matching'} {meta.total_count === 1 ? 'notification' : 'notifications'}
            {' · '}{Math.max(0, meta.total_count - ((page - 1) * PAGE_SIZE + items.length))} remaining after this page
          </p>
          <div className="flex gap-2">
            <button className="btn btn-outline btn-sm" disabled={loading || page <= 1} onClick={() => setPage(value => value - 1)}>Previous page</button>
            <button className="btn btn-primary btn-sm" disabled={loading || !meta.has_next} onClick={() => setPage(value => value + 1)}>Next page</button>
          </div>
        </nav>
      )}
      <details className="rounded-sm border border-fd-line bg-fd-panel p-3" open={searchParams.has('delivery') || undefined}>
        <summary className="cursor-pointer text-sm font-medium text-slate-300">Background email activity</summary>
        <BackgroundEmailActivity />
      </details>
    </div>
  );
}
