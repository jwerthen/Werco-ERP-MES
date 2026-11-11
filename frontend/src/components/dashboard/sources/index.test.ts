/**
 * The keyed loader calls every spec as `spec.fetch({ forced })`. A spec that passed an api
 * method by reference (`fetch: api.getActiveDowntime`) would hand that object to the
 * endpoint as its first argument — a filter or a page size the user never chose. Every
 * spec must wrap its call, so the options never reach the request.
 */
import api from '../../../services/api';
import { resetNotificationListCache } from '../widgets/extra/notificationListCache';
import { SOURCE_SPECS, getSourceSpec } from './index';

jest.mock('../../../services/api', () => {
  const methods = [
    'getQualitySummary',
    'getEquipmentDueSoon',
    'getLowStockAlerts',
    'getCapacityHeatmap',
    'getWallboard',
    'browseWorkOrders',
    'getOperationalInbox',
    'getActiveDowntime',
    'getMaintenanceDashboard',
    'getReceivingStats',
    'getInspectionQueue',
    'getKPIDashboard',
    'getVisitorLogs',
    'getNotifications',
  ];
  return {
    __esModule: true,
    default: Object.fromEntries(methods.map(name => [name, jest.fn(() => Promise.resolve(name))])),
  };
});

const apiMocks = api as unknown as Record<string, jest.Mock>;

beforeEach(() => {
  jest.clearAllMocks();
  resetNotificationListCache();
});

describe('SOURCE_SPECS', () => {
  it.each(Object.keys(SOURCE_SPECS))('%s sends the same request whether or not the call is forced', async key => {
    const spec = getSourceSpec(key)!;
    await spec.fetch();
    await spec.fetch({ forced: true });
    await spec.fetch({ forced: false });
    const called = Object.entries(apiMocks).filter(([, mock]) => mock.mock.calls.length > 0);
    // Each source reads exactly one endpoint, and the loader's options never become its arguments.
    expect(called).toHaveLength(1);
    const [, mock] = called[0];
    const [plain, ...rest] = mock.mock.calls;
    rest.forEach(args => expect(args).toEqual(plain));
    mock.mock.calls
      .flat()
      .forEach(arg => expect(arg).not.toEqual(expect.objectContaining({ forced: expect.anything() })));
  });

  it('cannot be looked up through Object.prototype names', () => {
    expect(getSourceSpec('constructor')).toBeUndefined();
    expect(getSourceSpec('toString')).toBeUndefined();
    expect(getSourceSpec('__proto__')).toBeUndefined();
  });
});
