/**
 * The dashboard's keyed source loader: floors, forced refresh, hidden tabs, needed-key
 * changes, per-key sequences, terminal 403s and the module cache.
 */
import { act, renderHook } from '@testing-library/react';
import { SOURCE_SPECS } from './sources';
import {
  ERROR_RETRY_CAP_MS,
  clearDashboardSourceCache,
  toNeededKey,
  useDashboardSources,
  type UseDashboardSourcesOptions,
} from './useDashboardSources';
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

  it('treats a 403 as terminal until the session scope changes', async () => {
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
});
