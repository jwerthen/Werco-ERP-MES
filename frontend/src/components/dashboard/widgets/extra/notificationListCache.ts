/**
 * The Notifications widget's unread list ("top 5 unread") is refetched when the shared
 * notification store's unread count changes, or after a floor — never on every page tick.
 *
 * The page's keyed loader only knows time floors, so the `notifications` source spec runs
 * its fetch through this cache: the loader may call it often (its own floor is short so a
 * count change shows up promptly), but the network is hit only when
 *  - the widget reported a count change (`invalidateNotificationList`), or
 *  - the widget unmounted since the list was fetched (it invalidates on unmount: nothing
 *    watched the count meanwhile, so a list served on the next mount could contradict it), or
 *  - the call is `force`d (a user's retry / manual refresh), or
 *  - the last good result is older than `NOTIFICATION_LIST_FLOOR_MS`.
 * A failed fetch is replayed (not retried) for `NOTIFICATION_LIST_ERROR_RETRY_MS`, so an
 * outage cannot turn every WebSocket push into a request — but never to a forced call.
 *
 * Keyed by session scope (`getHankSessionScope`): a sign-out, user switch or company switch
 * never serves the previous principal's list. Module state, so it survives remounts of `/`.
 * The unread COUNT itself is not fetched here — it comes from `useNotificationState`, the
 * store the header bell already polls (no second poller).
 */
import { getHankSessionScope } from '../../../ai/hankSession';

/** Network floor for the list while the unread count is unchanged. */
export const NOTIFICATION_LIST_FLOOR_MS = 120_000;
/** After a failure, the error is replayed for this long unless the list is invalidated. */
export const NOTIFICATION_LIST_ERROR_RETRY_MS = 60_000;

interface CacheState {
  scope: string | null;
  data: unknown;
  hasData: boolean;
  dataEpoch: number;
  fetchedAt: number;
  error: unknown;
  hasError: boolean;
  errorEpoch: number;
  errorAt: number;
  inflight: Promise<unknown> | null;
  inflightEpoch: number;
}

let epoch = 0;
let cache: CacheState | null = null;

function emptyCache(scope: string | null): CacheState {
  return {
    scope,
    data: undefined,
    hasData: false,
    dataEpoch: -1,
    fetchedAt: 0,
    error: undefined,
    hasError: false,
    errorEpoch: -1,
    errorAt: 0,
    inflight: null,
    inflightEpoch: -1,
  };
}

/** The unread count changed (new notification, or one was read): the next fetch goes to the network. */
export function invalidateNotificationList(): void {
  epoch += 1;
}

export interface NotificationListFetchOptions {
  /**
   * A user's retry / manual refresh: skip the data floor and the error replay. A request
   * already in flight for the current epoch is still shared — it is the refetch.
   */
  force?: boolean;
}

/** Fetch the unread list through the cache. `load` performs the real request. */
export function fetchNotificationListThroughCache<T>(
  load: () => Promise<T>,
  { force = false }: NotificationListFetchOptions = {}
): Promise<T> {
  const scope = getHankSessionScope();
  if (!cache || cache.scope !== scope) cache = emptyCache(scope);
  const state = cache;
  const now = Date.now();

  if (state.inflight && state.inflightEpoch === epoch) return state.inflight as Promise<T>;
  if (!force && state.hasData && state.dataEpoch === epoch && now - state.fetchedAt < NOTIFICATION_LIST_FLOOR_MS) {
    return Promise.resolve(state.data as T);
  }
  if (
    !force &&
    state.hasError &&
    state.errorEpoch === epoch &&
    now - state.errorAt < NOTIFICATION_LIST_ERROR_RETRY_MS
  ) {
    return Promise.reject(state.error);
  }

  const requestEpoch = epoch;
  const request: Promise<T> = Promise.resolve()
    .then(load)
    .then(
      data => {
        if (cache === state) {
          state.data = data;
          state.hasData = true;
          state.dataEpoch = requestEpoch;
          state.fetchedAt = Date.now();
          state.hasError = false;
          state.error = undefined;
        }
        return data;
      },
      (error: unknown) => {
        if (cache === state) {
          state.error = error;
          state.hasError = true;
          state.errorEpoch = requestEpoch;
          state.errorAt = Date.now();
        }
        throw error;
      }
    )
    .finally(() => {
      if (cache === state && state.inflight === request) state.inflight = null;
    });
  state.inflight = request;
  state.inflightEpoch = requestEpoch;
  return request;
}

/** Test seam: forget everything (including the invalidation epoch). */
export function resetNotificationListCache(): void {
  cache = null;
  epoch = 0;
}
