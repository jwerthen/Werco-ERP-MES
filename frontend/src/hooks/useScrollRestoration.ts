import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';

/**
 * useScrollRestoration — manual window-scroll restoration for the app shell.
 *
 * react-router's <ScrollRestoration> only works under a data router; this app
 * mounts a plain <BrowserRouter>, so we restore scroll ourselves. Call this hook
 * exactly ONCE, high in the tree (the Layout shell). It is a no-op-safe singleton:
 *
 *  - On every route change it first SAVES the *previous* route's window.scrollY to
 *    sessionStorage, keyed by that route's pathname+search.
 *  - Then it RESTORES the new route: if we have a saved position for its key, jump
 *    there; otherwise (a forward navigation to a route we've not seen) scroll to top.
 *
 * It only touches window scroll — it deliberately does not try to restore in-page
 * scroll containers (virtualized tables, side panels), which own their own state.
 *
 * sessionStorage (not localStorage) so positions are per-tab and cleared when the
 * tab closes, matching browser-native back/forward behavior.
 */

const STORAGE_PREFIX = 'scrollpos:';

const keyFor = (pathname: string, search: string): string => `${STORAGE_PREFIX}${pathname}${search}`;

const readSaved = (key: string): number | null => {
  try {
    const raw = sessionStorage.getItem(key);
    if (raw == null) return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  } catch {
    return null;
  }
};

const writeSaved = (key: string, y: number): void => {
  try {
    sessionStorage.setItem(key, String(y));
  } catch {
    // Storage may be unavailable (private mode / quota) — restoration is best-effort.
  }
};

export function useScrollRestoration(): void {
  const { pathname, search } = useLocation();
  // Track the previous route so we can persist *its* scroll position when we leave it.
  const previousKeyRef = useRef<string | null>(null);

  useEffect(() => {
    const currentKey = keyFor(pathname, search);

    // Save the outgoing route's position before we navigate away from it.
    const previousKey = previousKeyRef.current;
    if (previousKey && previousKey !== currentKey) {
      writeSaved(previousKey, window.scrollY);
    }

    // Restore the incoming route: saved position if we have one, else top.
    const saved = readSaved(currentKey);
    window.scrollTo(0, saved ?? 0);

    previousKeyRef.current = currentKey;
  }, [pathname, search]);
}

export default useScrollRestoration;
