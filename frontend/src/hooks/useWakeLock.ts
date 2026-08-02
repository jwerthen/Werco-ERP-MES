/**
 * useWakeLock — keep the display awake while a station surface (kiosk /
 * wallboard) is mounted, via the Screen Wake Lock API.
 *
 * Requests a `'screen'` lock on mount, re-acquires on `visibilitychange`
 * (the browser auto-releases the lock whenever the tab is hidden), and
 * releases + removes the listener on unmount. Rejections are swallowed —
 * the browser may deny the lock (low battery, power-save policy) and that
 * is normal operation, not an error.
 *
 * The Wake Lock API requires a secure context (HTTPS or localhost), so on a
 * plain-HTTP LAN deploy `navigator.wakeLock` is undefined and this hook is a
 * deliberate no-op — the station falls back to the device's display settings.
 */
import { useEffect } from 'react';

export function useWakeLock(): void {
  useEffect(() => {
    if (!('wakeLock' in navigator)) return undefined;

    let sentinel: WakeLockSentinel | null = null;
    let cancelled = false;
    let acquiring = false;

    const acquire = async () => {
      // Coalesce overlapping requests: a visibility flip while the mount
      // request is still in flight must not fire a second one — the loser's
      // sentinel would be overwritten and its lock leaked past unmount.
      if (acquiring) return;
      acquiring = true;
      try {
        const lock = await navigator.wakeLock.request('screen');
        if (cancelled) {
          // Unmounted while the request was in flight — let the lock go.
          lock.release().catch(() => undefined);
          return;
        }
        // Belt-and-braces: on the hidden->visible path the old sentinel was
        // auto-released by the browser, but never leak one that wasn't.
        sentinel?.release().catch(() => undefined);
        sentinel = lock;
      } catch {
        // Denied (low battery, browser policy) — normal, run without it.
      } finally {
        acquiring = false;
      }
    };

    const onVisibilityChange = () => {
      // Locks auto-release on tab hide; take a fresh one when we come back.
      if (document.visibilityState === 'visible') void acquire();
    };

    void acquire();
    document.addEventListener('visibilitychange', onVisibilityChange);

    return () => {
      cancelled = true;
      document.removeEventListener('visibilitychange', onVisibilityChange);
      sentinel?.release().catch(() => undefined);
      sentinel = null;
    };
  }, []);
}

export default useWakeLock;
