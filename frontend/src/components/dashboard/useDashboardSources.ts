/**
 * The dashboard's ONE keyed source loader.
 *
 * The page passes the sorted, comma-joined list of source keys its visible widgets need
 * (`neededKey` — a string, never a Set identity, so a re-resolve with the same widgets
 * changes nothing). Per key the loader keeps its own state, its own request sequence and
 * the time its last fetch started, so:
 *
 * - a fetch's result is accepted only against ITS key's sequence — a WebSocket tick that
 *   refreshes other sources can never discard an in-flight wallboard response;
 * - non-forced ticks (30 s poll, WebSocket pushes) skip a key younger than its
 *   `minIntervalMs`, never supersede a fetch still in flight until it is older than
 *   `max(minIntervalMs, IN_FLIGHT_GRACE_MS)` (then it is treated as hung and replaced), and
 *   skip ENTIRELY while the tab is hidden; a forced (manual) refresh bypasses the floor and
 *   any fetch in flight, but not `hardMinIntervalMs`;
 * - after an error the next tick retries once `min(minIntervalMs, 60 s)` has elapsed;
 * - a key that becomes needed is fetched immediately (unless the module cache can seed it,
 *   below); a key that stops being needed has its state and its per-key meta dropped, and
 *   its in-flight result is not applied — so a widget removed and re-added starts clean
 *   (loading, never the old "not available" or error) while it is asked again;
 * - HTTP 403 is terminal (`forbidden`) for the key while this mount keeps it — a remount, or
 *   removing and re-adding the widget, asks the server again — and the session scope change
 *   resets it;
 * - specs with `cacheTtlMs` keep their last result in a module-level cache keyed by the
 *   session scope, so the floor survives remounts of `/` (the landing page). A 403 EVICTS
 *   the data (refused data is never re-served as verified), and a failed refresh is
 *   remembered, so a remount inside the TTL shows the same "could not refresh" marker and
 *   keeps the error-retry cadence instead of passing the last result off as fresh. Every
 *   outcome is written when the request SETTLES, even if its result is no longer current
 *   (the user left `/`, or removed the widget, while it was in flight), ordered by attempt so
 *   a late answer never overwrites a newer one. A mount that needs a key while a newer
 *   attempt of it is still in flight follows that attempt's outcome, and while it is younger
 *   than the in-flight grace also adopts it for the floors instead of sending a duplicate
 *   (see `seedFromCache`).
 *
 * Api methods take no AbortSignal; the per-key sequence is what discards stale results.
 * Every fetch tells its spec whether it is `forced` (a user's refresh / retry), so a source
 * with a cache of its own (notifications) answers a manual refresh from the network.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { apiErrorStatus } from '../../utils/apiError';
import { getSourceSpec, SOURCE_SPECS } from './sources';
import { EMPTY_SOURCE_STATE, type DashboardSourceKey, type SourceSpec, type SourceState } from './widgetTypes';

/** After a failure, a key is retried no later than this, whatever its floor. */
export const ERROR_RETRY_CAP_MS = 60_000;
/**
 * A non-forced tick never supersedes a fetch in flight younger than `max(minIntervalMs, this)`;
 * past that age the fetch is treated as hung and replaced.
 */
export const IN_FLIGHT_GRACE_MS = 30_000;
export const SOURCE_ERROR_MESSAGE = 'Could not refresh';

/**
 * The cache is written when a request SETTLES, whether or not its result is still current
 * (the user may have left `/`, or removed the widget, while it was in flight) — the cache
 * outlives the mount. Results can settle out of order, so every write — success, failure and
 * refusal alike — is ordered by ATTEMPT: one module-wide counter stamped when a fetch starts.
 * Never by clock time — two fetches can start in the same millisecond, and the wall clock can
 * step backwards. Times are kept for what is genuinely about time: the TTL and `updatedAt`
 * (`verifiedAt`), the floors, the error-retry cadence and the in-flight grace (`startedAt`).
 *
 * Per cache key there are three records, each holding the newest attempt of its kind that no
 * newer attempt has superseded: the verified data (`sourceCache`), a failure NEWER than that
 * data (`failedAttempts` — kept even when there is no data yet, so an older success answering
 * late is cached with the failure still marked, never clean), and a refusal (`refusedAttempts`).
 */
interface CacheEntry {
  data: unknown;
  /** The attempt that produced `data`, and when it started / answered. */
  attempt: number;
  startedAt: number;
  verifiedAt: number;
}

interface AttemptMark {
  attempt: number;
  startedAt: number;
}

type FetchOutcome = 'success' | 'failure' | 'refused';

/**
 * Cached-source fetches still in flight, by cache key, across hook instances: a remount of `/`
 * can start while the previous mount's request has not answered yet, and follows (and, while it
 * is young enough, adopts) it rather than duplicating it. `settled` resolves with the outcome
 * once that fetch's result has been offered to the cache (an outcome older than what the cache
 * already holds is not written).
 */
interface PendingFetch extends AttemptMark {
  settled: Promise<FetchOutcome>;
}

const sourceCache = new Map<string, CacheEntry>();
/** The newest FAILED (non-403) attempt per cache key that no newer success has superseded. */
const failedAttempts = new Map<string, AttemptMark>();
/** The newest attempt the server REFUSED (403) per cache key, until a newer attempt succeeds. */
const refusedAttempts = new Map<string, number>();
const pendingFetches = new Map<string, PendingFetch>();
let attemptCounter = 0;
/** Bumped by every clear: a request started before a clear never writes into the cache after it. */
let cacheGeneration = 0;

/** Drop every cached source result (session scope changes, tests). */
export function clearDashboardSourceCache(): void {
  sourceCache.clear();
  failedAttempts.clear();
  refusedAttempts.clear();
  pendingFetches.clear();
  cacheGeneration += 1;
}

/** A success replaces older data unless a newer attempt was refused; it clears older failures and refusals. */
function recordCachedSuccess(cacheKey: string, generation: number, next: CacheEntry): void {
  if (generation !== cacheGeneration) return;
  if ((refusedAttempts.get(cacheKey) ?? -1) > next.attempt) return;
  const entry = sourceCache.get(cacheKey);
  if (entry && entry.attempt > next.attempt) return;
  sourceCache.set(cacheKey, next);
  refusedAttempts.delete(cacheKey);
  const failure = failedAttempts.get(cacheKey);
  if (failure && failure.attempt < next.attempt) failedAttempts.delete(cacheKey);
}

/** A non-403 failure is remembered unless a newer attempt already succeeded or failed. */
function recordCachedFailure(cacheKey: string, generation: number, mark: AttemptMark): void {
  if (generation !== cacheGeneration) return;
  const entry = sourceCache.get(cacheKey);
  if (entry && entry.attempt > mark.attempt) return;
  const failure = failedAttempts.get(cacheKey);
  if (failure && failure.attempt > mark.attempt) return;
  failedAttempts.set(cacheKey, mark);
}

/**
 * A 403 evicts the data — the server refused this session, so no remount may re-serve the
 * payload as verified — and is remembered, so an OLDER attempt answering late can never
 * re-cache what a newer one was refused. A refusal older than the cached data is stale (a newer
 * attempt has since succeeded) and changes nothing.
 */
function recordCachedRefusal(cacheKey: string, generation: number, attempt: number): void {
  if (generation !== cacheGeneration) return;
  const entry = sourceCache.get(cacheKey);
  if (entry && entry.attempt > attempt) return;
  sourceCache.delete(cacheKey);
  refusedAttempts.set(cacheKey, Math.max(attempt, refusedAttempts.get(cacheKey) ?? -1));
}

const cacheKeyFor = (scope: string | null, key: string) => `${scope ?? ''}\u0000${key}`;

interface SourceMeta {
  seq: number;
  inFlight: boolean;
  lastStartedAt: number | null;
  lastFailed: boolean;
  forbidden: boolean;
}

export function parseNeededKey(neededKey: string): string[] {
  return neededKey ? neededKey.split(',').filter(Boolean) : [];
}

/** Build the loader's `neededKey` from any list of keys: sorted, de-duplicated, comma-joined. */
export function toNeededKey(keys: readonly string[]): string {
  return Array.from(new Set(keys)).sort().join(',');
}

const isTabHidden = () => typeof document !== 'undefined' && document.visibilityState !== 'visible';

export interface UseDashboardSourcesOptions {
  /** The session scope (`getHankSessionScope()`); a change clears all state, floors and the cache. */
  scope?: string | null;
  /** Override the spec table (tests). */
  specs?: Partial<Record<string, SourceSpec>>;
}

export interface DashboardSourcesResult {
  /** One entry per needed key; a key not fetched yet reads as loading. */
  sources: Partial<Record<DashboardSourceKey, SourceState>>;
  /** Refresh the needed keys. `forced` = a user action. Resolves when the started fetches settle. */
  refresh: (forced: boolean) => Promise<void>;
}

export function useDashboardSources(
  neededKey: string,
  options: UseDashboardSourcesOptions = {}
): DashboardSourcesResult {
  const { scope = null, specs = SOURCE_SPECS } = options;
  const specsRef = useRef(specs);
  specsRef.current = specs;
  const scopeRef = useRef(scope);
  const [states, setStates] = useState<Record<string, SourceState>>({});
  const metaRef = useRef(new Map<string, SourceMeta>());
  const neededRef = useRef<string[]>([]);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const metaFor = useCallback((key: string): SourceMeta => {
    let meta = metaRef.current.get(key);
    if (!meta) {
      meta = { seq: 0, inFlight: false, lastStartedAt: null, lastFailed: false, forbidden: false };
      metaRef.current.set(key, meta);
    }
    return meta;
  }, []);

  /**
   * HTTP 403: the payload is dropped and no poll or manual refresh asks again while this mount
   * keeps the key. A remount, removing and re-adding the widget, or a session change asks again.
   */
  const showRefused = useCallback((key: string, meta: SourceMeta) => {
    meta.forbidden = true;
    setStates(previous => ({
      ...previous,
      [key]: { data: null, error: null, loading: false, forbidden: true, updatedAt: null },
    }));
  }, []);

  /** Any other failure: keep the last verified data, mark it, retry on the error cadence from `startedAt`. */
  const showFailed = useCallback((key: string, meta: SourceMeta, startedAt: number) => {
    meta.lastStartedAt = startedAt;
    meta.lastFailed = true;
    setStates(previous => {
      const before = previous[key];
      return {
        ...previous,
        [key]: {
          data: before?.data ?? null,
          error: SOURCE_ERROR_MESSAGE,
          loading: false,
          forbidden: false,
          updatedAt: before?.updatedAt ?? null,
        },
      };
    });
  }, []);

  const fetchKey = useCallback(
    (key: string, forced = false): Promise<void> => {
      const spec = getSourceSpec(key, specsRef.current);
      const meta = metaFor(key);
      const seq = ++meta.seq;
      const startedAt = Date.now();
      meta.inFlight = true;
      meta.lastStartedAt = startedAt;
      const fetchScope = scopeRef.current;
      const cacheKey = cacheKeyFor(fetchScope, key);
      const isCurrent = () => mountedRef.current && metaRef.current.get(key) === meta && meta.seq === seq;

      let request: Promise<unknown>;
      try {
        if (!spec) throw new Error(`No source spec for ${key}`);
        request = Promise.resolve(spec.fetch({ forced }));
      } catch (error) {
        request = Promise.reject(error);
      }

      const cached = Boolean(spec?.cacheTtlMs);
      const attempt = ++attemptCounter;
      const generation = cacheGeneration;
      const settled: Promise<FetchOutcome> = request.then(
        data => {
          const verifiedAt = Date.now();
          if (cached) recordCachedSuccess(cacheKey, generation, { data, attempt, startedAt, verifiedAt });
          if (!isCurrent()) return 'success';
          meta.inFlight = false;
          meta.lastFailed = false;
          setStates(previous => ({
            ...previous,
            [key]: { data, error: null, loading: false, forbidden: false, updatedAt: new Date(verifiedAt) },
          }));
          return 'success';
        },
        error => {
          const forbidden = apiErrorStatus(error) === 403;
          if (cached) {
            if (forbidden) recordCachedRefusal(cacheKey, generation, attempt);
            else recordCachedFailure(cacheKey, generation, { attempt, startedAt });
          }
          if (!isCurrent()) return forbidden ? 'refused' : 'failure';
          meta.inFlight = false;
          if (forbidden) {
            showRefused(key, meta);
            return 'refused';
          }
          showFailed(key, meta, startedAt);
          return 'failure';
        }
      );
      if (cached) {
        const pending: PendingFetch = { attempt, startedAt, settled };
        pendingFetches.set(cacheKey, pending);
        const done = () => {
          if (pendingFetches.get(cacheKey) === pending) pendingFetches.delete(cacheKey);
        };
        void settled.then(done, done);
      }
      return settled.then(() => undefined);
    },
    [metaFor, showRefused, showFailed]
  );

  /**
   * Seed a newly needed key from the module cache instead of fetching it. Returns false when
   * there is nothing to seed from, and the caller fetches.
   *
   * - Fresh data (inside `cacheTtlMs`) is shown at once. A failure newer than it comes back as
   *   it was left: the data with the "could not refresh" marker, retried on the error cadence
   *   from the failed attempt.
   * - A NEWER attempt still in flight (the previous mount's, or this mount's from before the
   *   widget was removed) is FOLLOWED: its outcome is applied once it settles — unless this
   *   mount has fetched the key itself since. A refusal (403) shows as refused at once;
   *   otherwise the cache is re-read, and if it holds no fresh data a failure still shows
   *   its marker. So seeded data is never left on screen as clean after a newer attempt of
   *   it failed or was refused.
   * - While that attempt is younger than the in-flight grace (`max(minIntervalMs, 30 s)`, the
   *   age at which a poll would replace it as hung) it is also ADOPTED rather than duplicated:
   *   its start drives the floors, so a forced refresh still waits out a hard minimum and a
   *   poll does not supersede it early. Older, it is treated as hung: with fresh data it is
   *   only followed and the floors run from the data; with none the key is fetched.
   */
  const seedRef = useRef<(key: string) => boolean>(() => false);
  const seedFromCache = useCallback(
    (key: string): boolean => {
      const spec = getSourceSpec(key, specsRef.current);
      if (!spec?.cacheTtlMs) return false;
      const cacheKey = cacheKeyFor(scopeRef.current, key);
      const now = Date.now();
      const stored = sourceCache.get(cacheKey);
      const entry = stored && now - stored.verifiedAt < spec.cacheTtlMs ? stored : undefined;
      const failure = failedAttempts.get(cacheKey);
      const failedSinceData = entry && failure && failure.attempt > entry.attempt ? failure : undefined;
      // Compared against every SETTLED attempt, not just the fresh data: in the microtask gap
      // between a fetch's cache write and its pendingFetches cleanup, the pending entry is that
      // already-settled attempt and must be neither followed nor adopted.
      const newestSettled = Math.max(
        stored?.attempt ?? -1,
        failure?.attempt ?? -1,
        refusedAttempts.get(cacheKey) ?? -1
      );
      const pending = pendingFetches.get(cacheKey);
      const newer = pending && pending.attempt > newestSettled ? pending : undefined;
      const adopt = Boolean(newer && now - newer.startedAt < Math.max(spec.minIntervalMs, IN_FLIGHT_GRACE_MS));
      if (!entry && !adopt) return false;

      const meta = metaFor(key);
      if (entry) {
        meta.lastStartedAt = failedSinceData?.startedAt ?? entry.startedAt;
        meta.lastFailed = Boolean(failedSinceData);
        setStates(previous => ({
          ...previous,
          [key]: {
            data: entry.data,
            error: failedSinceData ? SOURCE_ERROR_MESSAGE : null,
            loading: false,
            forbidden: false,
            updatedAt: new Date(entry.verifiedAt),
          },
        }));
      }
      if (!newer) return true;

      if (adopt) {
        meta.inFlight = true;
        meta.lastStartedAt = newer.startedAt;
      }
      const followedSeq = meta.seq;
      const apply = (outcome: unknown) => {
        if (!mountedRef.current || metaRef.current.get(key) !== meta || meta.seq !== followedSeq) return;
        meta.inFlight = false;
        if (outcome === 'refused') {
          showRefused(key, meta);
          return;
        }
        // A success of the newest attempt is always cached fresh, so the re-seed shows it.
        if (!seedRef.current(key) && outcome === 'failure') showFailed(key, meta, newer.startedAt);
      };
      void newer.settled.then(apply, apply);
      return true;
    },
    [metaFor, showRefused, showFailed]
  );
  seedRef.current = seedFromCache;

  // One effect for both inputs, so a session-scope change and a needed-key change are
  // applied in a single, ordered pass. A LAYOUT effect on purpose: the fetches for newly
  // needed keys start in the same commit that first renders their widgets (as the page's
  // single parallel load did before customization), so a widget is never on screen with
  // its request not yet issued, and a cached result is in place before the first paint.
  useLayoutEffect(() => {
    let previous = neededRef.current;
    if (scopeRef.current !== scope) {
      scopeRef.current = scope;
      clearDashboardSourceCache();
      metaRef.current.clear();
      setStates({});
      previous = [];
    }
    const next = parseNeededKey(neededKey);
    neededRef.current = next;
    const removed = previous.filter(key => !next.includes(key));
    const added = next.filter(key => !previous.includes(key));
    if (removed.length > 0) {
      removed.forEach(key => metaRef.current.delete(key));
      setStates(current => {
        const copy = { ...current };
        removed.forEach(key => delete copy[key]);
        return copy;
      });
    }
    added.forEach(key => {
      if (!seedFromCache(key)) void fetchKey(key);
    });
  }, [neededKey, scope, fetchKey, seedFromCache]);

  const shouldFetch = useCallback((key: string, forced: boolean, now: number): boolean => {
    const spec = getSourceSpec(key, specsRef.current);
    if (!spec) return false;
    const meta = metaRef.current.get(key);
    if (meta?.forbidden) return false;
    const lastStartedAt = meta?.lastStartedAt ?? null;
    if (lastStartedAt === null) return true;
    const age = now - lastStartedAt;
    if (forced) return !(spec.hardMinIntervalMs && age < spec.hardMinIntervalMs);
    if (meta?.inFlight && age < Math.max(spec.minIntervalMs, IN_FLIGHT_GRACE_MS)) return false;
    if (meta?.lastFailed) return age >= Math.min(spec.minIntervalMs, ERROR_RETRY_CAP_MS);
    return age >= spec.minIntervalMs;
  }, []);

  const refresh = useCallback(
    async (forced: boolean) => {
      if (!forced && isTabHidden()) return;
      const now = Date.now();
      const due = neededRef.current.filter(key => shouldFetch(key, forced, now));
      await Promise.allSettled(due.map(key => fetchKey(key, forced)));
    },
    [fetchKey, shouldFetch]
  );

  const sources = useMemo(() => {
    const out: Partial<Record<DashboardSourceKey, SourceState>> = {};
    parseNeededKey(neededKey).forEach(key => {
      out[key as DashboardSourceKey] = Object.prototype.hasOwnProperty.call(states, key)
        ? states[key]
        : EMPTY_SOURCE_STATE;
    });
    return out;
  }, [neededKey, states]);

  return { sources, refresh };
}
