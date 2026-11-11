import api from '../../../services/api';
import { resetNotificationListCache } from '../widgets/extra/notificationListCache';
import type { ExtraSourceKey } from '../widgetTypes';
import {
  EXTRA_SOURCE_SPECS,
  fetchNotificationsSource,
  NOTIFICATION_WIDGET_ROW_LIMIT,
  VISITOR_WIDGET_ROW_LIMIT,
  WORK_ORDER_WIDGET_ROW_LIMIT,
} from './extraSources';

jest.mock('../../../services/api', () => {
  const resolved = (label: string) => jest.fn(() => Promise.resolve(label));
  return {
    __esModule: true,
    default: {
      getWallboard: resolved('wallboard'),
      browseWorkOrders: resolved('browse'),
      getOperationalInbox: resolved('inbox'),
      getActiveDowntime: resolved('downtime'),
      getMaintenanceDashboard: resolved('maintenance'),
      getReceivingStats: resolved('receiving stats'),
      getInspectionQueue: resolved('inspection queue'),
      getKPIDashboard: resolved('kpis'),
      getVisitorLogs: resolved('visitors'),
      getNotifications: resolved('notifications'),
    },
  };
});

const ALL_KEYS: ExtraSourceKey[] = [
  'wallboard',
  'workOrdersOverdue',
  'workOrdersDueToday',
  'actionInbox',
  'downtime',
  'maintenance',
  'inspectionStats',
  'inspectionQueue',
  'deliveryKpis',
  'visitors',
  'notifications',
];

const MINUTE = 60_000;

beforeEach(() => {
  jest.clearAllMocks();
  resetNotificationListCache();
});

describe('EXTRA_SOURCE_SPECS', () => {
  it('has a labelled spec with a fetcher for every extra source key', () => {
    expect(Object.keys(EXTRA_SOURCE_SPECS).sort()).toEqual([...ALL_KEYS].sort());
    ALL_KEYS.forEach(key => {
      const spec = EXTRA_SOURCE_SPECS[key];
      expect(spec.label.trim()).not.toBe('');
      expect(typeof spec.fetch).toBe('function');
      expect(spec.minIntervalMs).toBeGreaterThanOrEqual(0);
    });
  });

  it('applies the documented floors and caches', () => {
    expect(EXTRA_SOURCE_SPECS.wallboard.minIntervalMs).toBe(MINUTE);
    expect(EXTRA_SOURCE_SPECS.workOrdersOverdue.minIntervalMs).toBe(MINUTE);
    expect(EXTRA_SOURCE_SPECS.workOrdersDueToday.minIntervalMs).toBe(MINUTE);
    expect(EXTRA_SOURCE_SPECS.downtime.minIntervalMs).toBe(MINUTE);
    expect(EXTRA_SOURCE_SPECS.inspectionStats.minIntervalMs).toBe(2 * MINUTE);
    expect(EXTRA_SOURCE_SPECS.inspectionQueue.minIntervalMs).toBe(2 * MINUTE);
    expect(EXTRA_SOURCE_SPECS.visitors.minIntervalMs).toBe(2 * MINUTE);

    expect(EXTRA_SOURCE_SPECS.actionInbox).toMatchObject({ minIntervalMs: 2 * MINUTE, cacheTtlMs: 2 * MINUTE });
    expect(EXTRA_SOURCE_SPECS.maintenance).toMatchObject({ minIntervalMs: 5 * MINUTE, cacheTtlMs: 5 * MINUTE });
    expect(EXTRA_SOURCE_SPECS.deliveryKpis).toMatchObject({
      minIntervalMs: 10 * MINUTE,
      cacheTtlMs: 10 * MINUTE,
      hardMinIntervalMs: MINUTE,
    });

    // Only the three expensive sources are cached; only the KPI call holds a hard minimum.
    const cached = ALL_KEYS.filter(key => EXTRA_SOURCE_SPECS[key].cacheTtlMs);
    expect(cached.sort()).toEqual(['actionInbox', 'deliveryKpis', 'maintenance']);
    const hardMin = ALL_KEYS.filter(key => EXTRA_SOURCE_SPECS[key].hardMinIntervalMs);
    expect(hardMin).toEqual(['deliveryKpis']);
  });

  it('reads the wallboard through the authenticated client', async () => {
    await expect(EXTRA_SOURCE_SPECS.wallboard.fetch()).resolves.toBe('wallboard');
    expect(api.getWallboard).toHaveBeenCalledWith();
  });

  it('includes COTS work orders so the list totals match the Overdue / Due Today tiles', async () => {
    await EXTRA_SOURCE_SPECS.workOrdersOverdue.fetch();
    await EXTRA_SOURCE_SPECS.workOrdersDueToday.fetch();
    expect(api.browseWorkOrders).toHaveBeenNthCalledWith(1, {
      scope: 'overdue',
      sort: 'due_date',
      direction: 'asc',
      limit: WORK_ORDER_WIDGET_ROW_LIMIT,
      hide_cots: false,
    });
    expect(api.browseWorkOrders).toHaveBeenNthCalledWith(2, {
      scope: 'due_today',
      sort: 'due_date',
      direction: 'asc',
      limit: WORK_ORDER_WIDGET_ROW_LIMIT,
      hide_cots: false,
    });
  });

  it('calls each remaining endpoint with the widget parameters', async () => {
    await EXTRA_SOURCE_SPECS.actionInbox.fetch();
    await EXTRA_SOURCE_SPECS.downtime.fetch();
    await EXTRA_SOURCE_SPECS.maintenance.fetch();
    await EXTRA_SOURCE_SPECS.inspectionStats.fetch();
    await EXTRA_SOURCE_SPECS.inspectionQueue.fetch();
    await EXTRA_SOURCE_SPECS.deliveryKpis.fetch();
    await EXTRA_SOURCE_SPECS.visitors.fetch();

    expect(api.getOperationalInbox).toHaveBeenCalledWith();
    expect(api.getActiveDowntime).toHaveBeenCalledWith();
    expect(api.getMaintenanceDashboard).toHaveBeenCalledWith();
    expect(api.getReceivingStats).toHaveBeenCalledWith(30);
    // No days_back: the queue must list every receipt still pending, like the stats badge.
    expect(api.getInspectionQueue).toHaveBeenCalledWith();
    expect(api.getKPIDashboard).toHaveBeenCalledWith({ period: '30d' });
    expect(api.getVisitorLogs).toHaveBeenCalledWith({ on_site_only: true, limit: VISITOR_WIDGET_ROW_LIMIT });
  });

  it('fetches the unread list through the notification cache', async () => {
    await expect(EXTRA_SOURCE_SPECS.notifications.fetch()).resolves.toBe('notifications');
    await expect(EXTRA_SOURCE_SPECS.notifications.fetch()).resolves.toBe('notifications');
    expect(api.getNotifications).toHaveBeenCalledTimes(1);
    expect(api.getNotifications).toHaveBeenCalledWith({ unread: true, pageSize: NOTIFICATION_WIDGET_ROW_LIMIT });
  });

  it('serves notifications through the forced-aware fetcher', () => {
    expect(EXTRA_SOURCE_SPECS.notifications.fetch).toBe(fetchNotificationsSource);
  });

  it('lets a forced notifications fetch past the replayed failure and the list floor', async () => {
    (api.getNotifications as jest.Mock).mockRejectedValueOnce(new Error('502'));
    await expect(fetchNotificationsSource()).rejects.toThrow('502');
    // An unforced call inside the replay window gets the replayed failure, no request.
    await expect(fetchNotificationsSource()).rejects.toThrow('502');
    expect(api.getNotifications).toHaveBeenCalledTimes(1);

    await expect(fetchNotificationsSource({ forced: true })).resolves.toBe('notifications');
    expect(api.getNotifications).toHaveBeenCalledTimes(2);
    // Inside the floor an unforced call is cached; a forced one refetches.
    await expect(fetchNotificationsSource()).resolves.toBe('notifications');
    expect(api.getNotifications).toHaveBeenCalledTimes(2);
    await expect(fetchNotificationsSource({ forced: true })).resolves.toBe('notifications');
    expect(api.getNotifications).toHaveBeenCalledTimes(3);
  });
});
