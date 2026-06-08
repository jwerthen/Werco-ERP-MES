/**
 * FEPERF-1 — Dashboard ETag cache invalidation after completion mutations.
 *
 * api.ts gained a private invalidateDashboardCache() that drops the cached
 * /shop-floor/dashboard ETag entry. It is invoked at the end of every
 * shop-floor / work-order mutation that changes dashboard state
 * (completeWorkOrder, clockOut, completeWOOperation, ... ).
 *
 * These tests exercise the PUBLIC surface only. They prove that once a
 * dashboard fetch has primed the ETag cache, a subsequent mutation forces the
 * next getDashboardWithCache() call to revalidate UNCONDITIONALLY — i.e. it is
 * sent WITHOUT an `If-None-Match` header — instead of serving a stale 304 body.
 *
 * axios is mocked at the module boundary: axios.create() returns a single
 * controllable instance whose .get/.post/.put we drive per-test, matching the
 * "mock at the boundary" pattern used elsewhere in the suite.
 */

// --- axios mock (must be declared before importing the module under test) ----
const mockGet = jest.fn();
const mockPost = jest.fn();
const mockPut = jest.fn();

const mockAxiosInstance = {
  get: mockGet,
  post: mockPost,
  put: mockPut,
  defaults: { headers: { common: {} as Record<string, string> } },
  interceptors: {
    request: { use: jest.fn() },
    response: { use: jest.fn() },
  },
};

jest.mock('axios', () => {
  const create = jest.fn(() => mockAxiosInstance);
  return {
    __esModule: true,
    default: { create, post: jest.fn() },
    create,
  };
});

// Imported AFTER the mock is registered so the singleton's constructor uses it.
import api from './api';

const DASHBOARD_URL = '/shop-floor/dashboard';

/** Mock a dashboard 200 response carrying an ETag header. */
function dashboard200(etag: string, data: unknown) {
  return { status: 200, data, headers: { etag } };
}

/** Mock a dashboard 304 (Not Modified) response. */
function dashboard304() {
  return { status: 304, data: '', headers: {} };
}

/** The fetchWithCache request config the implementation passes to axios.get. */
function lastDashboardGetConfig() {
  const call = mockGet.mock.calls.find(([url]) => url === DASHBOARD_URL);
  return call?.[1] as { headers?: Record<string, string> } | undefined;
}

function allDashboardGetConfigs() {
  return mockGet.mock.calls
    .filter(([url]) => url === DASHBOARD_URL)
    .map(([, cfg]) => cfg as { headers?: Record<string, string> } | undefined);
}

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPut.mockReset();
  // Drop any cross-test cache state from the module-global etagCache.
  api.clearCache();
});

describe('FEPERF-1: dashboard cache invalidation', () => {
  it('primes the dashboard ETag cache and sends If-None-Match on a follow-up fetch (baseline, no mutation)', async () => {
    mockGet
      .mockResolvedValueOnce(dashboard200('abc123', { open_work_orders: 3 }))
      .mockResolvedValueOnce(dashboard304());

    const first = await api.getDashboardWithCache();
    expect(first.fromCache).toBe(false);
    expect(first.data).toEqual({ open_work_orders: 3 });

    const second = await api.getDashboardWithCache();
    // Without any mutation, the cache survives: the second GET revalidates
    // conditionally and the 304 serves the cached body.
    expect(second.fromCache).toBe(true);
    expect(second.data).toEqual({ open_work_orders: 3 });

    const configs = allDashboardGetConfigs();
    expect(configs).toHaveLength(2);
    expect(configs[0]?.headers?.['If-None-Match']).toBeUndefined();
    expect(configs[1]?.headers?.['If-None-Match']).toBe('abc123');
  });

  it('drops the cached ETag after completeWorkOrder so the next dashboard fetch is unconditional', async () => {
    // 1. Prime the cache.
    mockGet.mockResolvedValueOnce(dashboard200('etag-v1', { open_work_orders: 5 }));
    await api.getDashboardWithCache();

    // 2. A successful completion mutation invalidates the dashboard cache.
    mockPost.mockResolvedValueOnce({ status: 200, data: { id: 7, status: 'complete' }, headers: {} });
    await api.completeWorkOrder(7, 10, 0);

    // 3. The next dashboard fetch must NOT carry If-None-Match — the stale
    //    etag was evicted, so this is an unconditional revalidation.
    mockGet.mockResolvedValueOnce(dashboard200('etag-v2', { open_work_orders: 4 }));
    const after = await api.getDashboardWithCache();

    expect(after.fromCache).toBe(false);
    expect(after.data).toEqual({ open_work_orders: 4 });

    const secondGet = allDashboardGetConfigs()[1];
    expect(secondGet?.headers?.['If-None-Match']).toBeUndefined();
  });

  it('drops the cached ETag after clockOut so the next dashboard fetch is unconditional', async () => {
    mockGet.mockResolvedValueOnce(dashboard200('clock-etag', { active_operators: 2 }));
    await api.getDashboardWithCache();

    mockPost.mockResolvedValueOnce({ status: 200, data: { id: 3 }, headers: {} });
    await api.clockOut(3, { quantity_produced: 12 });

    mockGet.mockResolvedValueOnce(dashboard200('clock-etag-2', { active_operators: 1 }));
    await api.getDashboardWithCache();

    const secondGet = allDashboardGetConfigs()[1];
    expect(secondGet?.headers?.['If-None-Match']).toBeUndefined();
  });

  it('invalidates the dashboard cache for the full set of completion-path mutations', async () => {
    type Mutation = { name: string; verb: 'post' | 'put'; run: () => Promise<unknown> };
    const mutations: Mutation[] = [
      { name: 'releaseWorkOrder', verb: 'post', run: () => api.releaseWorkOrder(1) },
      { name: 'startWorkOrder', verb: 'post', run: () => api.startWorkOrder(1) },
      { name: 'completeWorkOrder', verb: 'post', run: () => api.completeWorkOrder(1, 5, 0) },
      { name: 'startWOOperation', verb: 'post', run: () => api.startWOOperation(1) },
      { name: 'completeWOOperation', verb: 'post', run: () => api.completeWOOperation(1, 5, 0) },
      {
        name: 'clockIn',
        verb: 'post',
        run: () => api.clockIn({ work_order_id: 1, operation_id: 1, work_center_id: 1 }),
      },
      { name: 'clockOut', verb: 'post', run: () => api.clockOut(1, { quantity_produced: 1 }) },
      { name: 'startOperation', verb: 'put', run: () => api.startOperation(1) },
      { name: 'completeOperation', verb: 'post', run: () => api.completeOperation(1, { quantity_complete: 1 }) },
      {
        name: 'reportOperationProduction',
        verb: 'post',
        run: () => api.reportOperationProduction(1, { quantity_complete_delta: 1 }),
      },
      { name: 'holdOperation', verb: 'put', run: () => api.holdOperation(1) },
    ];

    for (const mutation of mutations) {
      mockGet.mockReset();
      mockPost.mockReset();
      mockPut.mockReset();
      api.clearCache();

      // Prime cache for this mutation.
      mockGet.mockResolvedValueOnce(dashboard200(`etag-${mutation.name}`, { tag: mutation.name }));
      await api.getDashboardWithCache();

      // Run the mutation.
      const verbMock = mutation.verb === 'post' ? mockPost : mockPut;
      verbMock.mockResolvedValueOnce({ status: 200, data: {}, headers: {} });
      await mutation.run();

      // Next dashboard fetch must be unconditional.
      mockGet.mockResolvedValueOnce(dashboard200(`etag-${mutation.name}-next`, { tag: 'next' }));
      await api.getDashboardWithCache();

      const config = lastDashboardGetConfig();
      expect(config?.headers?.['If-None-Match']).toBeUndefined();
    }
  });
});
