import {
  fetchNotificationListThroughCache,
  invalidateNotificationList,
  NOTIFICATION_LIST_ERROR_RETRY_MS,
  NOTIFICATION_LIST_FLOOR_MS,
  resetNotificationListCache,
} from './notificationListCache';

function fakeToken(sub: string, cid: number): string {
  const encode = (value: object) => btoa(JSON.stringify(value)).replace(/=+$/, '');
  return `${encode({ alg: 'HS256' })}.${encode({ sub, cid, type: 'access' })}.signature`;
}

let now = 1_000_000;

beforeEach(() => {
  resetNotificationListCache();
  sessionStorage.clear();
  now = 1_000_000;
  jest.spyOn(Date, 'now').mockImplementation(() => now);
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe('notificationListCache', () => {
  it('serves the cached list inside the floor and refetches after it', async () => {
    const load = jest.fn().mockResolvedValueOnce('first').mockResolvedValueOnce('second');
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('first');

    now += NOTIFICATION_LIST_FLOOR_MS - 1;
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('first');
    expect(load).toHaveBeenCalledTimes(1);

    now += 1;
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('second');
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('goes to the network as soon as the unread count changed', async () => {
    const load = jest.fn().mockResolvedValueOnce('before').mockResolvedValueOnce('after');
    await fetchNotificationListThroughCache(load);
    invalidateNotificationList();
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('after');
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('shares one in-flight request between callers', async () => {
    let resolve: (value: string) => void = () => undefined;
    const load = jest.fn(() => new Promise<string>(done => (resolve = done)));
    const first = fetchNotificationListThroughCache(load);
    const second = fetchNotificationListThroughCache(load);
    await Promise.resolve();
    resolve('list');
    await expect(first).resolves.toBe('list');
    await expect(second).resolves.toBe('list');
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('replays a failure instead of retrying it until the retry window passes', async () => {
    const error = new Error('offline');
    const load = jest.fn().mockRejectedValueOnce(error).mockResolvedValueOnce('recovered');
    await expect(fetchNotificationListThroughCache(load)).rejects.toBe(error);

    now += NOTIFICATION_LIST_ERROR_RETRY_MS - 1;
    await expect(fetchNotificationListThroughCache(load)).rejects.toBe(error);
    expect(load).toHaveBeenCalledTimes(1);

    now += 1;
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('recovered');
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('retries a failure at once when the count changed', async () => {
    const load = jest.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce('fresh');
    await expect(fetchNotificationListThroughCache(load)).rejects.toThrow('offline');
    invalidateNotificationList();
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('fresh');
  });

  it('lets a forced call past the error replay and the data floor', async () => {
    const error = new Error('offline');
    const load = jest
      .fn()
      .mockRejectedValueOnce(error)
      .mockResolvedValueOnce('recovered')
      .mockResolvedValueOnce('fresher');
    await expect(fetchNotificationListThroughCache(load)).rejects.toBe(error);

    // A user's retry inside the replay window still reaches the network.
    now += 1;
    await expect(fetchNotificationListThroughCache(load, { force: true })).resolves.toBe('recovered');
    // An unforced tick keeps the floor; a forced refresh inside it does not.
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('recovered');
    await expect(fetchNotificationListThroughCache(load, { force: true })).resolves.toBe('fresher');
    expect(load).toHaveBeenCalledTimes(3);
  });

  it('never serves the cached list as a success once a newer refresh of it failed', async () => {
    // Load OK, a manual refresh fails 5 s later, then a background tick 30 s after that —
    // all well inside the two-minute data floor. Serving the cached list to the tick would
    // read as a fresh success: the page would clear "could not refresh" and advance the
    // list's "updated" time without anything having reached the server.
    const error = new Error('offline');
    const load = jest
      .fn()
      .mockResolvedValueOnce('first')
      .mockRejectedValueOnce(error)
      .mockResolvedValueOnce('recovered');
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('first');

    now += 5_000;
    await expect(fetchNotificationListThroughCache(load, { force: true })).rejects.toBe(error);

    now += 30_000;
    await expect(fetchNotificationListThroughCache(load)).rejects.toBe(error);
    expect(load).toHaveBeenCalledTimes(2);

    // Past the error replay but still inside the data floor: retry the network rather than
    // fall back to the list the failure came after.
    now += NOTIFICATION_LIST_ERROR_RETRY_MS;
    expect(now - 1_000_000).toBeLessThan(NOTIFICATION_LIST_FLOOR_MS);
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('recovered');
    expect(load).toHaveBeenCalledTimes(3);

    // Recovered: the floor applies to the new list again.
    now += 1;
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('recovered');
    expect(load).toHaveBeenCalledTimes(3);
  });

  it('ignores a failure from an older count once the current count has a good list', async () => {
    let rejectOld: (reason: unknown) => void = () => undefined;
    const load = jest
      .fn()
      .mockImplementationOnce(() => new Promise<string>((_, reject) => (rejectOld = reject)))
      .mockResolvedValueOnce('current');
    const old = fetchNotificationListThroughCache(load);
    await Promise.resolve();

    invalidateNotificationList();
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('current');

    rejectOld(new Error('late failure for the old count'));
    await expect(old).rejects.toThrow('late failure');

    now += 1;
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('current');
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('a late result for an older count cannot unmask the current count failing', async () => {
    // Request A (old count) is slow; the count changes; the current count loads, then a manual
    // refresh of it fails. A then settles late — first as a failure, in the second run as a
    // success. Neither may clear the current failure marker and let the cached list through.
    for (const lateOutcome of ['fails', 'succeeds'] as const) {
      resetNotificationListCache();
      now = 1_000_000;
      let settleOld: { resolve: (v: string) => void; reject: (e: unknown) => void } = {
        resolve: () => undefined,
        reject: () => undefined,
      };
      const currentFailure = new Error('current refresh failed');
      const load = jest
        .fn()
        .mockImplementationOnce(() => new Promise<string>((resolve, reject) => (settleOld = { resolve, reject })))
        .mockResolvedValueOnce('current')
        .mockRejectedValueOnce(currentFailure);
      const old = fetchNotificationListThroughCache(load);
      await Promise.resolve();

      invalidateNotificationList();
      await expect(fetchNotificationListThroughCache(load)).resolves.toBe('current');
      now += 5_000;
      await expect(fetchNotificationListThroughCache(load, { force: true })).rejects.toBe(currentFailure);

      if (lateOutcome === 'fails') {
        settleOld.reject(new Error('late old failure'));
        await expect(old).rejects.toThrow('late old failure');
      } else {
        settleOld.resolve('old list');
        await expect(old).resolves.toBe('old list');
      }

      now += 30_000;
      await expect(fetchNotificationListThroughCache(load)).rejects.toBe(currentFailure);
      expect(load).toHaveBeenCalledTimes(3);
    }
  });

  it('a forced call shares a request already in flight rather than duplicating it', async () => {
    let resolve: (value: string) => void = () => undefined;
    const load = jest.fn(() => new Promise<string>(done => (resolve = done)));
    const tick = fetchNotificationListThroughCache(load);
    const forced = fetchNotificationListThroughCache(load, { force: true });
    await Promise.resolve();
    resolve('list');
    await expect(tick).resolves.toBe('list');
    await expect(forced).resolves.toBe('list');
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("never serves another session's list", async () => {
    sessionStorage.setItem('token', fakeToken('7', 1));
    const load = jest.fn().mockResolvedValueOnce('user 7').mockResolvedValueOnce('user 8');
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('user 7');

    sessionStorage.setItem('token', fakeToken('8', 1));
    await expect(fetchNotificationListThroughCache(load)).resolves.toBe('user 8');
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('drops a late result that arrives after the session changed', async () => {
    sessionStorage.setItem('token', fakeToken('7', 1));
    let resolve: (value: string) => void = () => undefined;
    const slow = jest.fn(() => new Promise<string>(done => (resolve = done)));
    const pending = fetchNotificationListThroughCache(slow);
    await Promise.resolve();

    sessionStorage.setItem('token', fakeToken('7', 2));
    const fresh = jest.fn().mockResolvedValue('company 2');
    await expect(fetchNotificationListThroughCache(fresh)).resolves.toBe('company 2');

    resolve('company 1');
    await expect(pending).resolves.toBe('company 1');
    // The company-2 cache is untouched by the late company-1 result.
    await expect(fetchNotificationListThroughCache(jest.fn())).resolves.toBe('company 2');
  });
});
