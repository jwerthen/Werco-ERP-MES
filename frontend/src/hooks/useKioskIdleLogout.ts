import { useCallback, useEffect, useRef, useState } from 'react';

interface UseKioskIdleLogoutOptions {
  /** Only arm the timer while an operator is logged in. */
  enabled: boolean;
  /** Total idle time before logout, in seconds. */
  timeoutSeconds: number;
  /** How long before logout the countdown warning shows (default 30s). */
  warningSeconds?: number;
  /** Fired exactly once when the idle deadline passes. */
  onTimeout: () => void;
}

const ACTIVITY_EVENTS: Array<keyof WindowEventMap> = ['pointerdown', 'keydown', 'touchstart', 'wheel'];

/**
 * Kiosk idle auto-logout with a visible countdown.
 *
 * Returns `countdownSeconds` — null until the warning window, then the whole
 * seconds remaining (ticking down each second). ANY activity (tap, key, scan)
 * resets the deadline, including during the countdown, so a gloved operator
 * just touches the screen to stay logged in.
 */
export function useKioskIdleLogout({ enabled, timeoutSeconds, warningSeconds = 30, onTimeout }: UseKioskIdleLogoutOptions): {
  countdownSeconds: number | null;
  reset: () => void;
} {
  const [countdownSeconds, setCountdownSeconds] = useState<number | null>(null);
  const deadlineRef = useRef<number>(0);
  const firedRef = useRef(false);
  const onTimeoutRef = useRef(onTimeout);
  onTimeoutRef.current = onTimeout;

  const reset = useCallback(() => {
    deadlineRef.current = Date.now() + timeoutSeconds * 1000;
    firedRef.current = false;
    setCountdownSeconds(null);
  }, [timeoutSeconds]);

  useEffect(() => {
    if (!enabled) {
      setCountdownSeconds(null);
      return undefined;
    }

    reset();

    const tick = () => {
      if (firedRef.current) return;
      const remainingMs = deadlineRef.current - Date.now();
      if (remainingMs <= 0) {
        firedRef.current = true;
        setCountdownSeconds(null);
        onTimeoutRef.current();
        return;
      }
      const remainingS = Math.ceil(remainingMs / 1000);
      setCountdownSeconds(remainingS <= warningSeconds ? remainingS : null);
    };

    const interval = window.setInterval(tick, 1000);
    const handleActivity = () => {
      if (!firedRef.current) reset();
    };
    ACTIVITY_EVENTS.forEach((event) => window.addEventListener(event, handleActivity));

    return () => {
      window.clearInterval(interval);
      ACTIVITY_EVENTS.forEach((event) => window.removeEventListener(event, handleActivity));
    };
  }, [enabled, warningSeconds, reset]);

  return { countdownSeconds, reset };
}
