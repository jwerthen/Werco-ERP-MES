/**
 * Source specs for the dashboard's opt-in widgets (`widgets/extra`). `sources/index.ts`
 * merges these with CORE_SOURCE_SPECS into a Record<DashboardSourceKey, SourceSpec>.
 *
 * Every source reads an endpoint that already exists; nothing here writes. The floors are
 * cost controls, not freshness promises:
 *  - `wallboard` is ONE request shared by Blocked Jobs, Shipping Outlook, Today's Output and
 *    Late Jobs (bounded, read-only, no reconcile on read).
 *  - The two work-order lists send `hide_cots: false` so their totals agree with the
 *    Overdue / Due Today tiles (which count COTS work orders and link with `cots=1`).
 *  - `actionInbox`, `maintenance` and `deliveryKpis` are expensive or slow-moving, so they
 *    keep a session-scoped cache (`cacheTtlMs`) that survives remounts of `/`;
 *    `deliveryKpis` (10 KPIs × current/prior/sparkline, uncached on the server) also holds
 *    a 60 s hard minimum against manual refreshes.
 *  - `notifications` fetches through `notificationListCache`, which hits the network only
 *    when the unread count changed or the list is two minutes old — see that module.
 */
import api from '../../../services/api';
import type { ExtraSourceDataMap } from '../../../types/dashboardWidgetData';
import { fetchNotificationListThroughCache } from '../widgets/extra/notificationListCache';
import type { ExtraSourceKey, SourceSpec } from '../widgetTypes';

const SECOND = 1_000;
const MINUTE = 60 * SECOND;

/** Rows the work-order list widgets show (the header count is the server's uncapped `total`). */
export const WORK_ORDER_WIDGET_ROW_LIMIT = 8;
/** Rows the Notifications widget lists. */
export const NOTIFICATION_WIDGET_ROW_LIMIT = 5;
/** Rows the Visitors On Site widget lists (the header count is the server's `total`). */
export const VISITOR_WIDGET_ROW_LIMIT = 10;

type ExtraSourceSpecs = { [K in ExtraSourceKey]: SourceSpec<ExtraSourceDataMap[K]> };

const SPECS: ExtraSourceSpecs = {
  wallboard: {
    label: 'Shop wallboard',
    fetch: () => api.getWallboard(),
    minIntervalMs: MINUTE,
  },
  workOrdersOverdue: {
    label: 'Overdue work orders',
    fetch: () =>
      api.browseWorkOrders({
        scope: 'overdue',
        sort: 'due_date',
        direction: 'asc',
        limit: WORK_ORDER_WIDGET_ROW_LIMIT,
        hide_cots: false,
      }),
    minIntervalMs: MINUTE,
  },
  workOrdersDueToday: {
    label: 'Work orders due today',
    fetch: () =>
      api.browseWorkOrders({
        scope: 'due_today',
        sort: 'due_date',
        direction: 'asc',
        limit: WORK_ORDER_WIDGET_ROW_LIMIT,
        hide_cots: false,
      }),
    minIntervalMs: MINUTE,
  },
  actionInbox: {
    label: 'Action inbox',
    fetch: () => api.getOperationalInbox(),
    minIntervalMs: 2 * MINUTE,
    cacheTtlMs: 2 * MINUTE,
  },
  downtime: {
    label: 'Active downtime',
    fetch: () => api.getActiveDowntime(),
    minIntervalMs: MINUTE,
  },
  maintenance: {
    label: 'Maintenance',
    fetch: () => api.getMaintenanceDashboard(),
    minIntervalMs: 5 * MINUTE,
    cacheTtlMs: 5 * MINUTE,
  },
  inspectionStats: {
    label: 'Receiving inspection',
    fetch: () => api.getReceivingStats(30),
    minIntervalMs: 2 * MINUTE,
  },
  inspectionQueue: {
    label: 'Inspection queue',
    fetch: () => api.getInspectionQueue(),
    minIntervalMs: 2 * MINUTE,
  },
  deliveryKpis: {
    label: 'Delivery KPIs',
    fetch: () => api.getKPIDashboard({ period: '30d' }),
    minIntervalMs: 10 * MINUTE,
    hardMinIntervalMs: MINUTE,
    cacheTtlMs: 10 * MINUTE,
  },
  visitors: {
    label: 'Visitors on site',
    fetch: () => api.getVisitorLogs({ on_site_only: true, limit: VISITOR_WIDGET_ROW_LIMIT }),
    minIntervalMs: 2 * MINUTE,
  },
  notifications: {
    label: 'Notifications',
    fetch: () =>
      fetchNotificationListThroughCache(() =>
        api.getNotifications({ unread: true, pageSize: NOTIFICATION_WIDGET_ROW_LIMIT })
      ),
    // Short on purpose: the real two-minute network floor lives in notificationListCache,
    // which also refetches as soon as the unread count changes. A long loader floor here
    // would swallow the widget's change-triggered refresh.
    minIntervalMs: SECOND,
  },
};

export const EXTRA_SOURCE_SPECS: Record<ExtraSourceKey, SourceSpec> = SPECS;
