/**
 * The dashboard's keyed source loader: floors, forced refresh, hidden tabs, needed-key
 * changes, per-key sequences, terminal 403s and the module cache.
 */
import { act, renderHook } from '@testing-library/react';
import api from '../../services/api';
import { SOURCE_SPECS } from './sources';
import {
  ERROR_RETRY_CAP_MS,
  IN_FLIGHT_GRACE_MS,
  clearDashboardSourceCache,
  toNeededKey,
  useDashboardSources,
  type UseDashboardSourcesOptions,
} from './useDashboardSources';
import { resetNotificationListCache } from './widgets/extra/notificationListCache';
import type { DashboardSourceKey, SourceSpec } from './widgetTypes';

jest.mock('../../services/api', () => ({ __esModule: true, default: {} }));

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const forbidden = () => Object.assign(new Error('Forbidden'), { response: { status: 403 } });

let now = 1_000_000;
let visibility: DocumentVisibilityState = 'visible';

beforeEach(() => {
  now = 1_000_000;
  visibility = 'visible';
  clearDashboardSourceCache();
  jest.spyOn(Date, 'now').mockImplementation(() => now);
  jest.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
});

afterEach(() => {
  jest.restoreAllMocks();
});

const spec = (fetch: jest.Mock, overrides: Partial<SourceSpec> = {}): SourceSpec => ({
  label: 'Test',
  fetch,
  minIntervalMs: 60_000,
  ...overrides,
});

function setup(initialKey: string, specs: Record<string, SourceSpec>, scope: string | null = 'scope-a') {
  return renderHook(
    ({ neededKey, scope: currentScope }: { neededKey: string; scope: string | null }) =>
      useDashboardSources(neededKey, { scope: currentScope, specs } as UseDashboardSourcesOptions),
    { initialProps: { neededKey: initialKey, scope } }
  );
}

const flush = () => act(async () => undefined);

describe('SOURCE_SPECS', () => {
  it('has a spec for every DashboardSourceKey', () => {
    // A Record over the union: the compiler fails this file when a key is added to or
    // removed from DashboardSourceKey without updating the list.
    const ALL_KEYS: Record<DashboardSourceKey, true> = {
      quality: true,
      equipment: true,
      stock: true,
      capacity: true,
      wallboard: true,
      workOrdersOverdue: true,
      workOrdersDueToday: true,
      actionInbox: true,
      downtime: true,
      maintenance: true,
      inspectionStats: true,
      inspectionQueue: true,
      deliveryKpis: true,
      visitors: true,
      notifications: true,
    };
    for (const key of Object.keys(ALL_KEYS) as DashboardSourceKey[]) {
      const sourceSpec = SOURCE_SPECS[key];
      expect({ key, hasSpec: Boolean(sourceSpec) }).toEqual({ key, hasSpec: true });
      expect(typeof sourceSpec.fetch).toBe('function');
      expect(sourceSpec.label.length).toBeGreaterThan(0);
      expect(sourceSpec.minIntervalMs).toBeGreaterThanOrEqual(0);
    }
  });
});

describe('toNeededKey', () => {
  it('sorts and de-duplicates so the same widgets always produce the same key', () => {
    expect(toNeededKey(['stock', 'quality', 'stock'])).toBe('quality,stock');
    expect(toNeededKey([])).toBe('');
  });
});

describe('useDashboardSources', () => {
  it('fetches needed keys immediately and reports a never-fetched key as loading', async () => {
    const pending = deferred<number>();
    const fetch = jest.fn(() => pending.promise);
    const { result } = setup('stock', { stock: spec(fetch) });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(result.current.sources.stock?.loading).toBe(true);
    await act(async () => pending.resolve(7));
    expect(result.current.sources.stock).toMatchObject({ data: 7, loading: false, error: null, forbidden: false });
  });

  it('honors the refresh floor for non-forced ticks; a forced refresh bypasses it', async () => {
    const fetch = jest.fn(() => Promise.resolve(1));
    const { result } = setup('stock', { stock: spec(fetch, { minIntervalMs: 60_000 }) });
    await flush();
    expect(fetch).toHaveBeenCalledTimes(1);

    now += 30_000;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(1);

    await act(() => result.current.refresh(true));
    expect(fetch).toHaveBeenCalledTimes(2);

    now += 60_000;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('keeps a hard minimum even for a forced refresh', async () => {
    const fetch = jest.fn(() => Promise.resolve(1));
    const { result } = setup('deliveryKpis', {
      deliveryKpis: spec(fetch, { minIntervalMs: 600_000, hardMinIntervalMs: 60_000 }),
    });
    await flush();
    now += 10_000;
    await act(() => result.current.refresh(true));
    expect(fetch).toHaveBeenCalledTimes(1);
    now += 60_000;
    await act(() => result.current.refresh(true));
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('skips every non-forced tick while the tab is hidden', async () => {
    const fetch = jest.fn(() => Promise.resolve(1));
    const { result } = setup('stock', { stock: spec(fetch, { minIntervalMs: 0 }) });
    await flush();
    visibility = 'hidden';
    now += 120_000;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(1);
    await act(() => result.current.refresh(true));
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('fetches an added key at once and drops a removed key (and its late result)', async () => {
    const stock = jest.fn(() => Promise.resolve('a'));
    const equipmentPending = deferred<string>();
    const equipment = jest.fn(() => equipmentPending.promise);
    const { result, rerender } = setup('stock', { stock: spec(stock), equipment: spec(equipment) });
    await flush();

    rerender({ neededKey: 'equipment,stock', scope: 'scope-a' });
    expect(equipment).toHaveBeenCalledTimes(1);
    expect(stock).toHaveBeenCalledTimes(1);
    expect(result.current.sources.equipment?.loading).toBe(true);

    rerender({ neededKey: 'stock', scope: 'scope-a' });
    expect(result.current.sources.equipment).toBeUndefined();
    await act(async () => equipmentPending.resolve('late'));
    expect(result.current.sources.equipment).toBeUndefined();
    expect(Object.keys(result.current.sources)).toEqual(['stock']);

    // Re-added: asked again, and the late answer for the removed key was never applied.
    equipment.mockImplementation(() => new Promise<string>(() => undefined));
    rerender({ neededKey: 'equipment,stock', scope: 'scope-a' });
    expect(equipment).toHaveBeenCalledTimes(2);
    expect(result.current.sources.equipment).toMatchObject({ data: null, loading: true });
  });

  it('a widget removed after a 403 and re-added starts clean while it is asked again', async () => {
    const equipment = jest
      .fn<Promise<unknown>, []>()
      .mockRejectedValueOnce(forbidden())
      .mockImplementation(() => new Promise(() => undefined));
    const { result, rerender } = setup('equipment', { equipment: spec(equipment) });
    await flush();
    expect(result.current.sources.equipment).toMatchObject({ forbidden: true, loading: false });

    rerender({ neededKey: '', scope: 'scope-a' });
    rerender({ neededKey: 'equipment', scope: 'scope-a' });
    expect(equipment).toHaveBeenCalledTimes(2);
    expect(result.current.sources.equipment).toMatchObject({ forbidden: false, loading: true, data: null });
  });

  it('a success after failures puts the source back on its normal floor', async () => {
    const fetch = jest
      .fn<Promise<unknown>, []>()
      .mockResolvedValueOnce('first')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue('recovered');
    const { result } = setup('deliveryKpis', { deliveryKpis: spec(fetch, { minIntervalMs: 600_000 }) });
    await flush();

    now += 600_000;
    await act(() => result.current.refresh(false));
    expect(result.current.sources.deliveryKpis).toMatchObject({ error: 'Could not refresh' });
    now += ERROR_RETRY_CAP_MS;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(result.current.sources.deliveryKpis).toMatchObject({ data: 'recovered', error: null });

    // Recovered: not every 60 s any more, but the 10-minute floor from the recovering fetch.
    now += ERROR_RETRY_CAP_MS;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(3);
    now += 600_000 - ERROR_RETRY_CAP_MS;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(4);
  });

  it('a hung retry is protected by the full floor when the floor is longer than the 30 s grace', async () => {
    const hung = deferred<unknown>();
    const fetch = jest
      .fn<Promise<unknown>, []>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockReturnValueOnce(hung.promise)
      .mockResolvedValue('replacement');
    const { result } = setup('maintenance', { maintenance: spec(fetch, { minIntervalMs: 120_000 }) });
    await flush();
    now += ERROR_RETRY_CAP_MS;
    act(() => {
      void result.current.refresh(false); // starts the retry, which hangs
    });
    expect(fetch).toHaveBeenCalledTimes(2);

    // 60 s into the hung retry: past 30 s, but younger than the 120 s floor — not replaced.
    now += 60_000;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(2);
    now += 60_000;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('never drops an in-flight result when another key refreshes (per-key sequence)', async () => {
    const wallboardPending = deferred<string>();
    const wallboard = jest.fn(() => wallboardPending.promise);
    const quality = jest.fn(() => Promise.resolve('q'));
    const { result } = setup('quality,wallboard', {
      wallboard: spec(wallboard, { minIntervalMs: 60_000 }),
      quality: spec(quality, { minIntervalMs: 0 }),
    });
    await flush();
    expect(wallboard).toHaveBeenCalledTimes(1);

    // A WebSocket push lands while the wallboard request is still in flight.
    now += 1_000;
    await act(async () => {
      void result.current.refresh(false);
    });
    expect(quality).toHaveBeenCalledTimes(2);
    expect(wallboard).toHaveBeenCalledTimes(1);

    await act(async () => wallboardPending.resolve('board'));
    expect(result.current.sources.wallboard).toMatchObject({ data: 'board', loading: false });
    expect(result.current.sources.quality).toMatchObject({ data: 'q' });
  });

  it('keeps the last verified data on an error and retries once min(floor, 60 s) has passed', async () => {
    const fetch = jest
      .fn<Promise<string>, []>()
      .mockResolvedValueOnce('first')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue('again');
    const { result } = setup('stock', { stock: spec(fetch, { minIntervalMs: 300_000 }) });
    await flush();
    now += 300_000;
    await act(() => result.current.refresh(false));
    expect(result.current.sources.stock).toMatchObject({ data: 'first', error: 'Could not refresh', loading: false });

    now += ERROR_RETRY_CAP_MS - 1;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(2);
    now += 1;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(result.current.sources.stock).toMatchObject({ data: 'again', error: null });
  });

  it('treats a 403 as terminal for the mount (no poll or manual refresh re-asks) until the session scope changes', async () => {
    const fetch = jest.fn<Promise<string>, []>().mockRejectedValueOnce(forbidden()).mockResolvedValue('allowed');
    const { result, rerender } = setup('stock', { stock: spec(fetch, { minIntervalMs: 0 }) });
    await flush();
    expect(result.current.sources.stock).toMatchObject({ forbidden: true, error: null, data: null, loading: false });

    now += 600_000;
    await act(() => result.current.refresh(false));
    await act(() => result.current.refresh(true));
    expect(fetch).toHaveBeenCalledTimes(1);

    rerender({ neededKey: 'stock', scope: 'scope-b' });
    await flush();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(result.current.sources.stock).toMatchObject({ forbidden: false, data: 'allowed' });
  });

  it('serves a cacheTtlMs source from the module cache across remounts', async () => {
    const fetch = jest.fn(() => Promise.resolve({ value: 42 }));
    const specs = { deliveryKpis: spec(fetch, { minIntervalMs: 600_000, cacheTtlMs: 600_000 }) };
    const first = setup('deliveryKpis', specs);
    await flush();
    expect(fetch).toHaveBeenCalledTimes(1);
    first.unmount();

    now += 120_000;
    const second = setup('deliveryKpis', specs);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(second.result.current.sources.deliveryKpis).toMatchObject({ data: { value: 42 }, loading: false });
    // The floor still counts from the cached fetch: a tick before it has elapsed is skipped.
    await act(() => second.result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(1);
    second.unmount();

    // Another session never reads this session's cache.
    const otherScope = setup('deliveryKpis', specs, 'scope-b');
    await flush();
    expect(fetch).toHaveBeenCalledTimes(2);
    otherScope.unmount();

    now += 600_000;
    const stale = setup('deliveryKpis', specs, 'scope-b');
    await flush();
    expect(fetch).toHaveBeenCalledTimes(3);
    stale.unmount();
  });

  it('passes `forced` to the spec: true for a user refresh, false for first loads and background ticks', async () => {
    const fetch = jest.fn((_options?: { forced?: boolean }) => Promise.resolve(1));
    const { result } = setup('stock', { stock: spec(fetch, { minIntervalMs: 0 }) });
    await flush();
    expect(fetch).toHaveBeenLastCalledWith({ forced: false });
    await act(() => result.current.refresh(true));
    expect(fetch).toHaveBeenLastCalledWith({ forced: true });
    now += 1;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenLastCalledWith({ forced: false });
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('lets a user refresh reach the network for the notifications list, past its replayed failure', async () => {
    // The page's "Retry unavailable sections" and header Refresh are forced refreshes. The
    // list cache replays a failure for 60 s to anything not forced, so unless the loader
    // passes `forced` through, those buttons re-serve the same error without a request.
    const getNotifications = jest.fn().mockRejectedValueOnce(new Error('502')).mockResolvedValue({ items: [] });
    const mockedApi = api as unknown as Record<string, unknown>;
    mockedApi.getNotifications = getNotifications;
    resetNotificationListCache();
    try {
      const { result } = setup('notifications', { notifications: SOURCE_SPECS.notifications });
      await flush();
      expect(getNotifications).toHaveBeenCalledTimes(1);
      expect(result.current.sources.notifications).toMatchObject({ error: 'Could not refresh', data: null });

      now += 5_000; // past the loader's own floor, well inside the list cache's error replay
      await act(() => result.current.refresh(true));
      expect(getNotifications).toHaveBeenCalledTimes(2);
      expect(result.current.sources.notifications).toMatchObject({ error: null, data: { items: [] } });
    } finally {
      delete mockedApi.getNotifications;
      resetNotificationListCache();
    }
  });

  it('a forced refresh supersedes a fetch in flight, and the superseded answer is never adopted', async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const fetch = jest
      .fn<Promise<string>, []>()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    const { result } = setup('stock', { stock: spec(fetch) });
    act(() => {
      void result.current.refresh(true);
    });
    expect(fetch).toHaveBeenCalledTimes(2);
    await act(async () => second.resolve('fresh'));
    expect(result.current.sources.stock).toMatchObject({ data: 'fresh', loading: false });
    await act(async () => first.resolve('superseded'));
    expect(result.current.sources.stock).toMatchObject({ data: 'fresh' });
  });

  it('replaces a hung request on a background tick only once the in-flight grace has passed', async () => {
    const fetch = jest
      .fn<Promise<string>, []>()
      .mockImplementationOnce(() => new Promise<string>(() => undefined))
      .mockResolvedValue('recovered');
    const { result } = setup('stock', { stock: spec(fetch, { minIntervalMs: 0 }) });
    now += IN_FLIGHT_GRACE_MS - 1;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(1);
    now += 1;
    await act(() => result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(result.current.sources.stock).toMatchObject({ data: 'recovered', loading: false, error: null });
  });

  it('on a session change, never shows the previous session’s data, its late answer or its cache', async () => {
    const lateFromA = deferred<string>();
    const firstForB = deferred<string>();
    const fetch = jest
      .fn<Promise<string>, []>()
      .mockResolvedValueOnce('company-a')
      .mockImplementationOnce(() => lateFromA.promise)
      .mockImplementationOnce(() => firstForB.promise)
      .mockResolvedValue('company-a-again');
    const specs = { maintenance: spec(fetch, { minIntervalMs: 300_000, cacheTtlMs: 600_000 }) };
    const { result, rerender, unmount } = setup('maintenance', specs, 'scope-a');
    await flush();
    expect(result.current.sources.maintenance).toMatchObject({ data: 'company-a' });

    // A refresh for company A is still in flight when the session switches to company B.
    now += 1_000;
    act(() => {
      void result.current.refresh(true);
    });
    rerender({ neededKey: 'maintenance', scope: 'scope-b' });
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(result.current.sources.maintenance).toMatchObject({ data: null, loading: true, error: null });

    await act(async () => lateFromA.resolve('company-a-late'));
    expect(result.current.sources.maintenance).toMatchObject({ data: null, loading: true });

    await act(async () => firstForB.resolve('company-b'));
    expect(result.current.sources.maintenance).toMatchObject({ data: 'company-b', loading: false });
    unmount();

    // Back in company A inside the TTL: the switch dropped A's cache, so it is fetched again.
    now += 1_000;
    const back = setup('maintenance', specs, 'scope-a');
    expect(fetch).toHaveBeenCalledTimes(4);
    expect(back.result.current.sources.maintenance).toMatchObject({ data: null, loading: true });
    await flush();
    expect(back.result.current.sources.maintenance).toMatchObject({ data: 'company-a-again' });
    back.unmount();
  });

  it('never re-serves a cached result the server has since refused (403) after a remount', async () => {
    const fetch = jest.fn<Promise<unknown>, []>().mockResolvedValueOnce({ value: 42 }).mockRejectedValue(forbidden());
    const specs = { maintenance: spec(fetch, { minIntervalMs: 300_000, cacheTtlMs: 600_000 }) };
    const first = setup('maintenance', specs);
    await flush();
    expect(first.result.current.sources.maintenance).toMatchObject({ data: { value: 42 }, forbidden: false });

    // Mid-session the role loses access (the session scope does not change).
    now += 10_000;
    await act(() => first.result.current.refresh(true));
    expect(first.result.current.sources.maintenance).toMatchObject({ data: null, forbidden: true });
    first.unmount();

    // Back on `/` well inside the TTL: the refused payload is never hydrated as verified data.
    now += 10_000;
    const second = setup('maintenance', specs);
    expect(second.result.current.sources.maintenance?.data).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(3);
    await flush();
    expect(second.result.current.sources.maintenance).toMatchObject({ data: null, forbidden: true, loading: false });
    second.unmount();
  });

  it('keeps a failed refresh marked after a remount, and its retry cadence, instead of re-serving the cache as fresh', async () => {
    const fetch = jest
      .fn<Promise<unknown>, []>()
      .mockResolvedValueOnce('first')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue('again');
    const specs = {
      deliveryKpis: spec(fetch, { minIntervalMs: 600_000, hardMinIntervalMs: 60_000, cacheTtlMs: 600_000 }),
    };
    const first = setup('deliveryKpis', specs);
    await flush();
    now += 60_000;
    await act(() => first.result.current.refresh(true));
    expect(first.result.current.sources.deliveryKpis).toMatchObject({ data: 'first', error: 'Could not refresh' });
    first.unmount();

    now += 10_000;
    const second = setup('deliveryKpis', specs);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(second.result.current.sources.deliveryKpis).toMatchObject({
      data: 'first',
      error: 'Could not refresh',
      loading: false,
    });
    // The error retry counts from the FAILED attempt: min(floor, 60 s) after it, not the floor after the success.
    now += ERROR_RETRY_CAP_MS - 10_000 - 1;
    await act(() => second.result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(2);
    now += 1;
    await act(() => second.result.current.refresh(false));
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(second.result.current.sources.deliveryKpis).toMatchObject({ data: 'again', error: null });
    second.unmount();

    // A success clears the marker for the next remount.
    const third = setup('deliveryKpis', specs);
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(third.result.current.sources.deliveryKpis).toMatchObject({ data: 'again', error: null });
    third.unmount();
  });

  describe('a cached source whose refresh settles after its result stopped being current', () => {
    const inboxSpecs = (fetch: jest.Mock) => ({
      actionInbox: spec(fetch, { minIntervalMs: 120_000, cacheTtlMs: 120_000 }),
    });

    it('records a refresh that fails after the page was left, so coming back still shows it failed', async () => {
      const refresh = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(refresh.promise)
        .mockResolvedValue('again');
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();

      // A manual refresh at t=30 s, then the user navigates away before it answers.
      now += 30_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      expect(fetch).toHaveBeenCalledTimes(2);
      first.unmount();
      refresh.reject(new Error('offline'));
      await flush();

      // Back on `/` inside the TTL: the newest attempt failed, so the cache is not passed off as fresh.
      now += 30_000;
      const second = setup('actionInbox', specs);
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(second.result.current.sources.actionInbox).toMatchObject({
        data: 'first',
        error: 'Could not refresh',
        loading: false,
      });
      // And the retry counts from the failed attempt (t=30 s), not the floor after the success.
      now += ERROR_RETRY_CAP_MS - 30_000 - 1;
      await act(() => second.result.current.refresh(false));
      expect(fetch).toHaveBeenCalledTimes(2);
      now += 1;
      await act(() => second.result.current.refresh(false));
      expect(fetch).toHaveBeenCalledTimes(3);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'again', error: null });
      second.unmount();
    });

    it('records a refresh that fails after its widget was removed, so re-adding it shows the failure', async () => {
      const refresh = deferred<unknown>();
      const fetch = jest.fn<Promise<unknown>, []>().mockResolvedValueOnce('first').mockReturnValueOnce(refresh.promise);
      const hook = setup('actionInbox', inboxSpecs(fetch));
      await flush();

      now += 30_000;
      act(() => {
        void hook.result.current.refresh(true);
      });
      hook.rerender({ neededKey: '', scope: 'scope-a' });
      refresh.reject(new Error('offline'));
      await flush();

      now += 10_000;
      hook.rerender({ neededKey: 'actionInbox', scope: 'scope-a' });
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(hook.result.current.sources.actionInbox).toMatchObject({ data: 'first', error: 'Could not refresh' });
      hook.unmount();
    });

    it('a remount that hydrated while the refresh was still in flight learns that it failed', async () => {
      const refresh = deferred<unknown>();
      const fetch = jest.fn<Promise<unknown>, []>().mockResolvedValueOnce('first').mockReturnValueOnce(refresh.promise);
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();

      now += 30_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      first.unmount();

      // A quick Back: the new mount hydrates the (still clean) entry before the refresh answers.
      now += 1_000;
      const second = setup('actionInbox', specs);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'first', error: null });

      refresh.reject(new Error('offline'));
      await flush();
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'first', error: 'Could not refresh' });
      second.unmount();
    });

    it('a remount that hydrated while the refresh was in flight shows a refusal at once, not after re-asking', async () => {
      const refresh = deferred<unknown>();
      const fetch = jest.fn<Promise<unknown>, []>().mockResolvedValueOnce('first').mockReturnValueOnce(refresh.promise);
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();

      now += 30_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      first.unmount();
      now += 1_000;
      const second = setup('actionInbox', specs);
      expect(second.result.current.sources.actionInbox?.data).toBe('first');

      refresh.reject(forbidden());
      await flush();
      // Refused for this session: the payload is dropped and the key is terminal, with no
      // second request whose wait (or whose own failure) would keep refused data on screen.
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: null, forbidden: true });
      now += 600_000;
      await act(() => second.result.current.refresh(true));
      expect(fetch).toHaveBeenCalledTimes(2);
      second.unmount();
    });

    it('shows the failure even when the entry aged past its TTL before the in-flight refresh failed', async () => {
      const refresh = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(refresh.promise)
        .mockResolvedValue('again');
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();

      now += 100_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      first.unmount();
      now += 10_000;
      const second = setup('actionInbox', specs);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'first', error: null });

      // t=121 s: past the 120 s TTL when the t=100 s refresh fails.
      now += 11_000;
      refresh.reject(new Error('offline'));
      await flush();
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'first', error: 'Could not refresh' });
      // Retried on the error cadence from the failed attempt (t=100 s + 60 s), not at once.
      now += 160_000 - 121_000 - 1;
      await act(() => second.result.current.refresh(false));
      expect(fetch).toHaveBeenCalledTimes(2);
      now += 1;
      await act(() => second.result.current.refresh(false));
      expect(fetch).toHaveBeenCalledTimes(3);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'again', error: null });
      second.unmount();
    });

    it('records a success that answers after the page was left, so an older late failure cannot mark it failed', async () => {
      const older = deferred<unknown>();
      const newer = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(newer.promise);
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();

      // Two refreshes in flight; the second supersedes the first. Then the user leaves.
      now += 30_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      now += 1_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      first.unmount();

      newer.resolve('fresh');
      await flush();
      older.reject(new Error('offline'));
      await flush();

      now += 5_000;
      const second = setup('actionInbox', specs);
      expect(fetch).toHaveBeenCalledTimes(3);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'fresh', error: null });
      second.unmount();
    });

    it('never re-caches what a newer attempt was refused when an older attempt answers late', async () => {
      const older = deferred<unknown>();
      const newer = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(newer.promise)
        .mockRejectedValue(forbidden());
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();

      now += 30_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      now += 1_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      newer.reject(forbidden());
      await flush();
      expect(first.result.current.sources.actionInbox).toMatchObject({ data: null, forbidden: true });
      first.unmount();

      older.resolve('refused payload');
      await flush();

      now += 5_000;
      const second = setup('actionInbox', specs);
      expect(second.result.current.sources.actionInbox?.data).not.toBe('refused payload');
      await flush();
      expect(fetch).toHaveBeenCalledTimes(4);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: null, forbidden: true });
      second.unmount();
    });

    it('orders results by attempt, not by clock: a refresh started after the clock stepped back still counts', async () => {
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockRejectedValueOnce(new Error('offline'));
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();

      now -= 10_000; // the wall clock steps backwards
      await act(() => first.result.current.refresh(true));
      first.unmount();

      const second = setup('actionInbox', specs);
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'first', error: 'Could not refresh' });
      second.unmount();
    });

    it('a remount that fetched the key itself since hydrating ignores the older in-flight outcome', async () => {
      const refresh = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(refresh.promise)
        .mockResolvedValueOnce('own');
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();

      now += 30_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      first.unmount();
      now += 1_000;
      const second = setup('actionInbox', specs);
      now += 1_000;
      await act(() => second.result.current.refresh(true));
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'own', error: null });

      refresh.reject(forbidden());
      await flush();
      // The older attempt's refusal is stale against this mount's own newer answer, which stands
      // on screen and in the cache.
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'own', error: null, forbidden: false });
      second.unmount();
      const third = setup('actionInbox', specs);
      expect(fetch).toHaveBeenCalledTimes(3);
      expect(third.result.current.sources.actionInbox).toMatchObject({ data: 'own', error: null });
      third.unmount();
    });

    it('a remount during the first load adopts the request in flight instead of sending a duplicate', async () => {
      const firstLoad = deferred<unknown>();
      const fetch = jest.fn<Promise<unknown>, []>().mockReturnValueOnce(firstLoad.promise);
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      expect(fetch).toHaveBeenCalledTimes(1);
      first.unmount();

      now += 2_000;
      const second = setup('actionInbox', specs);
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(second.result.current.sources.actionInbox).toMatchObject({ loading: true, data: null });
      // A background tick inside the in-flight grace does not supersede the adopted request either.
      now += 10_000;
      await act(() => second.result.current.refresh(false));
      expect(fetch).toHaveBeenCalledTimes(1);

      firstLoad.resolve('loaded');
      await flush();
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'loaded', error: null, loading: false });
      second.unmount();
    });

    it('a remount adopts the request in flight for the floors: a forced refresh still waits out the hard minimum', async () => {
      const refresh = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(refresh.promise)
        .mockResolvedValue('later');
      const specs = {
        deliveryKpis: spec(fetch, { minIntervalMs: 600_000, hardMinIntervalMs: 60_000, cacheTtlMs: 600_000 }),
      };
      const first = setup('deliveryKpis', specs);
      await flush();
      now += 60_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      expect(fetch).toHaveBeenCalledTimes(2);
      first.unmount();

      // Back 10 s later: 'first' is shown, and the t=60 s request is what the hard minimum counts from.
      now += 10_000;
      const second = setup('deliveryKpis', specs);
      expect(second.result.current.sources.deliveryKpis).toMatchObject({ data: 'first', error: null });
      await act(() => second.result.current.refresh(true));
      expect(fetch).toHaveBeenCalledTimes(2);

      refresh.resolve('second');
      await flush();
      expect(second.result.current.sources.deliveryKpis).toMatchObject({ data: 'second', error: null });
      now += 50_000; // 60 s after the adopted request started
      await act(() => second.result.current.refresh(true));
      expect(fetch).toHaveBeenCalledTimes(3);
      second.unmount();
    });

    it('re-adding a widget while its refresh is in flight adopts that refresh rather than sending another', async () => {
      const refresh = deferred<unknown>();
      const fetch = jest.fn<Promise<unknown>, []>().mockResolvedValueOnce('first').mockReturnValueOnce(refresh.promise);
      const hook = setup('actionInbox', inboxSpecs(fetch));
      await flush();
      now += 30_000;
      act(() => {
        void hook.result.current.refresh(true);
      });
      hook.rerender({ neededKey: '', scope: 'scope-a' });
      now += 1_000;
      hook.rerender({ neededKey: 'actionInbox', scope: 'scope-a' });
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(hook.result.current.sources.actionInbox).toMatchObject({ data: 'first', error: null });

      refresh.resolve('refreshed');
      await flush();
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(hook.result.current.sources.actionInbox).toMatchObject({ data: 'refreshed', error: null });
      hook.unmount();
    });

    it('remembers a failure with no data to mark yet, so an older success answering late is cached as failed', async () => {
      const firstLoad = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockReturnValueOnce(firstLoad.promise)
        .mockRejectedValueOnce(new Error('offline'))
        .mockResolvedValue('again');
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);

      // The first load is slow; a manual refresh supersedes it and fails at once.
      now += 5_000;
      await act(() => first.result.current.refresh(true));
      expect(first.result.current.sources.actionInbox).toMatchObject({ data: null, error: 'Could not refresh' });
      // The first load then answers. It is older than the failure.
      firstLoad.resolve('older');
      await flush();
      first.unmount();

      now += 10_000;
      const second = setup('actionInbox', specs);
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'older', error: 'Could not refresh' });
      // Retried on the error cadence from the failed attempt (t=5 s + 60 s).
      now += ERROR_RETRY_CAP_MS - 10_000 - 1;
      await act(() => second.result.current.refresh(false));
      expect(fetch).toHaveBeenCalledTimes(2);
      now += 1;
      await act(() => second.result.current.refresh(false));
      expect(fetch).toHaveBeenCalledTimes(3);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'again', error: null });
      second.unmount();
    });

    it('a refusal older than the cached data is stale and evicts nothing', async () => {
      const older = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(older.promise)
        .mockResolvedValueOnce('newer');
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();

      now += 30_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      now += 1_000;
      await act(() => first.result.current.refresh(true));
      expect(first.result.current.sources.actionInbox).toMatchObject({ data: 'newer', error: null });
      older.reject(forbidden());
      await flush();
      expect(first.result.current.sources.actionInbox).toMatchObject({ data: 'newer', forbidden: false });
      first.unmount();

      now += 5_000;
      const second = setup('actionInbox', specs);
      expect(fetch).toHaveBeenCalledTimes(3);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'newer', error: null });
      second.unmount();
    });

    it('never marks a newer cached success failed because an older refresh failed late', async () => {
      const stale = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(stale.promise)
        .mockResolvedValueOnce('fresh');
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();

      now += 30_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      first.unmount();

      // Back after the TTL, with the t=30 s refresh past the in-flight grace (hung): a new fetch
      // (t=160 s) succeeds and is cached.
      now += 130_000;
      const second = setup('actionInbox', specs);
      await flush();
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'fresh', error: null });
      second.unmount();

      // The t=30 s refresh only now fails: older evidence than the cached success.
      stale.reject(new Error('offline'));
      await flush();

      now += 1_000;
      const third = setup('actionInbox', specs);
      expect(fetch).toHaveBeenCalledTimes(3);
      expect(third.result.current.sources.actionInbox).toMatchObject({ data: 'fresh', error: null });
      third.unmount();
    });

    describe('a request too old to adopt is still followed', () => {
      // t=0 first load is slow; t=5 s a manual refresh supersedes it; the first load answers at
      // t=20 s (cached, not shown: it is not current); the refresh hangs; the user leaves at
      // t=60 s and returns at t=135 s — the data is fresh (115 s old) and the refresh 130 s old,
      // clearly past the 120 s in-flight grace, so it is followed but not adopted.
      async function returnWhileOldRefreshHangs() {
        const firstLoad = deferred<unknown>();
        const refresh = deferred<unknown>();
        const fetch = jest
          .fn<Promise<unknown>, []>()
          .mockReturnValueOnce(firstLoad.promise)
          .mockReturnValueOnce(refresh.promise)
          .mockResolvedValue('later');
        const specs = inboxSpecs(fetch);
        const first = setup('actionInbox', specs);
        now += 5_000;
        act(() => {
          void first.result.current.refresh(true);
        });
        now += 15_000;
        firstLoad.resolve('first');
        await flush();
        now += 40_000;
        first.unmount();
        now += 75_000;
        const second = setup('actionInbox', specs);
        expect(fetch).toHaveBeenCalledTimes(2);
        expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'first', error: null });
        return { refresh, second };
      }

      it('shows its refusal when it answers 403', async () => {
        const { refresh, second } = await returnWhileOldRefreshHangs();
        refresh.reject(forbidden());
        await flush();
        expect(second.result.current.sources.actionInbox).toMatchObject({ data: null, forbidden: true });
        second.unmount();
      });

      it('shows its failure when it fails', async () => {
        const { refresh, second } = await returnWhileOldRefreshHangs();
        refresh.reject(new Error('gateway timeout'));
        await flush();
        expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'first', error: 'Could not refresh' });
        second.unmount();
      });
    });

    it('keeps the newer of two successes that answer out of order', async () => {
      const older = deferred<unknown>();
      const newer = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(older.promise)
        .mockReturnValueOnce(newer.promise);
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();
      now += 30_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      now += 1_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      first.unmount();

      newer.resolve('newer');
      await flush();
      older.resolve('older');
      await flush();

      now += 5_000;
      const second = setup('actionInbox', specs);
      expect(fetch).toHaveBeenCalledTimes(3);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'newer', error: null });
      second.unmount();
    });

    it('remembers the newest refusal when an older one answers after it', async () => {
      const a2 = deferred<unknown>();
      const a3 = deferred<unknown>();
      const a4 = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(a2.promise)
        .mockReturnValueOnce(a3.promise)
        .mockReturnValueOnce(a4.promise)
        .mockRejectedValue(forbidden());
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();
      for (let i = 0; i < 3; i += 1) {
        now += 1_000;
        act(() => {
          void first.result.current.refresh(true);
        });
      }
      first.unmount();

      // The newest (a4) is refused, then the older a2 is refused late, then a3 answers late.
      a4.reject(forbidden());
      await flush();
      a2.reject(forbidden());
      await flush();
      a3.resolve('payload from before the newest refusal');
      await flush();

      now += 5_000;
      const second = setup('actionInbox', specs);
      expect(second.result.current.sources.actionInbox?.data).toBeNull();
      expect(fetch).toHaveBeenCalledTimes(5);
      await flush();
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: null, forbidden: true });
      second.unmount();
    });

    it('an older failure answering late does not pull the error retry earlier', async () => {
      const f2 = deferred<unknown>();
      const f3 = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(f2.promise)
        .mockReturnValueOnce(f3.promise)
        .mockResolvedValue('again');
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();
      now += 10_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      now += 40_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      now += 1_000;
      first.unmount();

      f3.reject(new Error('offline')); // the newest failure, started at t=50 s
      await flush();
      f2.reject(new Error('offline')); // an older one, started at t=10 s, answering late
      await flush();

      now += 9_000; // t=60 s
      const second = setup('actionInbox', specs);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'first', error: 'Could not refresh' });
      // Retry is due 60 s after the NEWEST failed attempt (t=110 s), not after the older one (t=70 s).
      now += 10_000;
      await act(() => second.result.current.refresh(false));
      expect(fetch).toHaveBeenCalledTimes(3);
      now += 40_000;
      await act(() => second.result.current.refresh(false));
      expect(fetch).toHaveBeenCalledTimes(4);
      second.unmount();
    });

    it('adopts an in-flight request older than 30 s while it is younger than the floor', async () => {
      const refresh = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(refresh.promise)
        .mockResolvedValue('later');
      const specs = {
        deliveryKpis: spec(fetch, { minIntervalMs: 600_000, hardMinIntervalMs: 60_000, cacheTtlMs: 600_000 }),
      };
      const first = setup('deliveryKpis', specs);
      await flush();
      now += 60_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      first.unmount();

      // Back 40 s into the request: past 30 s, but the grace is max(10 min floor, 30 s).
      now += 40_000;
      const second = setup('deliveryKpis', specs);
      await act(() => second.result.current.refresh(true));
      expect(fetch).toHaveBeenCalledTimes(2);
      second.unmount();
    });

    it('an adopted request counts as in flight: a poll does not duplicate it on the error cadence', async () => {
      const retry = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockRejectedValueOnce(new Error('offline'))
        .mockReturnValueOnce(retry.promise)
        .mockResolvedValue('later');
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();
      now += 10_000;
      await act(() => first.result.current.refresh(true));
      now += 10_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      now += 1_000;
      first.unmount();

      // Back at t=25 s: the data carries the t=10 s failure, and the t=20 s retry is adopted.
      now += 4_000;
      const second = setup('actionInbox', specs);
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'first', error: 'Could not refresh' });
      // A poll 60 s into the adopted retry (the error cadence) must not send a duplicate.
      now += 55_000;
      await act(() => second.result.current.refresh(false));
      expect(fetch).toHaveBeenCalledTimes(3);
      second.unmount();
    });

    it("a follow registered before the widget was removed never touches the re-added widget's own result", async () => {
      const refresh = deferred<unknown>();
      const fetch = jest
        .fn<Promise<unknown>, []>()
        .mockResolvedValueOnce('first')
        .mockReturnValueOnce(refresh.promise)
        .mockResolvedValueOnce('own');
      const specs = inboxSpecs(fetch);
      const first = setup('actionInbox', specs);
      await flush();
      now += 30_000;
      act(() => {
        void first.result.current.refresh(true);
      });
      first.unmount();

      now += 1_000;
      const second = setup('actionInbox', specs);
      // Remove and re-add (the re-add follows the same request again), then fetch its own.
      second.rerender({ neededKey: '', scope: 'scope-a' });
      second.rerender({ neededKey: 'actionInbox', scope: 'scope-a' });
      now += 1_000;
      await act(() => second.result.current.refresh(true));
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'own', error: null });

      refresh.reject(forbidden());
      await flush();
      expect(second.result.current.sources.actionInbox).toMatchObject({ data: 'own', forbidden: false });
      second.unmount();
    });
  });
});
