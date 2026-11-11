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
 *   `minIntervalMs`, never supersede a fetch still in flight, and skip ENTIRELY while the
 *   tab is hidden; a forced (manual) refresh bypasses the floor but not `hardMinIntervalMs`;
 * - after an error the next tick retries once `min(minIntervalMs, 60 s)` has elapsed;
 * - a key that becomes needed is fetched immediately; a key that stops being needed has
 *   its state dropped (so the page's "widget errors" banner never names a source nothing
 *   shows) and any in-flight result for it is ignored;
 * - HTTP 403 is terminal (`forbidden`) until the session scope changes;
 * - specs with `cacheTtlMs` keep their last result in a module-level cache keyed by the
 *   session scope, so the floor survives remounts of `/` (the landing page). A 403 EVICTS
 *   the entry (a remount refetches and is refused again — refused data is never re-served
 *   as verified), and a failed refresh is recorded on it, so a remount shows the same
 *   "could not refresh" marker and keeps the error-retry cadence instead of passing the
 *   last result off as fresh.
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
/** A non-forced tick never supersedes a fetch younger than this (a hung request is replaced after it). */
export const IN_FLIGHT_GRACE_MS = 30_000;
export const SOURCE_ERROR_MESSAGE = 'Could not refresh';

interface CacheEntry {
  data: unknown;
  startedAt: number;
  verifiedAt: number;
  /** When the latest refresh after `verifiedAt` started, if it FAILED (non-403); null otherwise. */
  failedStartedAt: number | null;
}

const sourceCache = new Map<string, CacheEntry>();

/** Drop every cached source result (session scope changes, tests). */
export function clearDashboardSourceCache(): void {
  sourceCache.clear();
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

  const fetchKey = useCallback(
    (key: string, forced = false): Promise<void> => {
      const spec = getSourceSpec(key, specsRef.current);
      const meta = metaFor(key);
      const seq = ++meta.seq;
      const startedAt = Date.now();
      meta.inFlight = true;
      meta.lastStartedAt = startedAt;
      const fetchScope = scopeRef.current;
      const isCurrent = () => mountedRef.current && metaRef.current.get(key) === meta && meta.seq === seq;

      let request: Promise<unknown>;
      try {
        if (!spec) throw new Error(`No source spec for ${key}`);
        request = Promise.resolve(spec.fetch({ forced }));
      } catch (error) {
        request = Promise.reject(error);
      }

      return request.then(
        data => {
          if (!isCurrent()) return;
          meta.inFlight = false;
          meta.lastFailed = false;
          const verifiedAt = Date.now();
          if (spec?.cacheTtlMs) {
            sourceCache.set(cacheKeyFor(fetchScope, key), { data, startedAt, verifiedAt, failedStartedAt: null });
          }
          setStates(previous => ({
            ...previous,
            [key]: { data, error: null, loading: false, forbidden: false, updatedAt: new Date(verifiedAt) },
          }));
        },
        error => {
          const forbidden = apiErrorStatus(error) === 403;
          const cacheKey = cacheKeyFor(fetchScope, key);
          // Evict even when this result is no longer current (the page unmounted, the key
          // went away): the server refused this session, so no remount may re-serve the
          // cached payload as verified. A later success simply re-caches it.
          if (forbidden && spec?.cacheTtlMs) sourceCache.delete(cacheKey);
          if (!isCurrent()) return;
          meta.inFlight = false;
          if (forbidden) {
            meta.forbidden = true;
            setStates(previous => ({
              ...previous,
              [key]: { data: null, error: null, loading: false, forbidden: true, updatedAt: null },
            }));
            return;
          }
          meta.lastFailed = true;
          const cached = spec?.cacheTtlMs ? sourceCache.get(cacheKey) : undefined;
          if (cached) sourceCache.set(cacheKey, { ...cached, failedStartedAt: startedAt });
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
        }
      );
    },
    [metaFor]
  );

  /**
   * Seed a newly needed key from the module cache when its result is still fresh. A failed
   * refresh recorded on the entry comes back as it was left: the last verified data with
   * the "could not refresh" marker, retried on the error cadence from the failed attempt.
   */
  const hydrateFromCache = useCallback(
    (key: string): boolean => {
      const spec = getSourceSpec(key, specsRef.current);
      if (!spec?.cacheTtlMs) return false;
      const entry = sourceCache.get(cacheKeyFor(scopeRef.current, key));
      if (!entry || Date.now() - entry.verifiedAt >= spec.cacheTtlMs) return false;
      const failed = entry.failedStartedAt !== null;
      const meta = metaFor(key);
      meta.lastStartedAt = entry.failedStartedAt ?? entry.startedAt;
      meta.lastFailed = failed;
      meta.inFlight = false;
      setStates(previous => ({
        ...previous,
        [key]: {
          data: entry.data,
          error: failed ? SOURCE_ERROR_MESSAGE : null,
          loading: false,
          forbidden: false,
          updatedAt: new Date(entry.verifiedAt),
        },
      }));
      return true;
    },
    [metaFor]
  );

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
      if (!hydrateFromCache(key)) void fetchKey(key);
    });
  }, [neededKey, scope, fetchKey, hydrateFromCache]);

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
