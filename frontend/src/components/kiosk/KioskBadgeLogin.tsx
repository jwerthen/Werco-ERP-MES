import React, { useCallback, useEffect, useState } from 'react';
import KioskKeypad from './KioskKeypad';
import { MAX_BADGE_LENGTH, useBadgeCapture } from './useBadgeCapture';
import { formatCentralTime } from '../../utils/centralTime';

interface KioskBadgeLoginProps {
  stationLabel: string;
  onLogin: (employeeId: string) => Promise<void>;
}

/**
 * Full-screen badge sign-in (Foundry 1a). A keyboard-wedge scanner "types" the
 * employee id and sends Enter — captured at the window level so it works
 * without any focused input (gloved operators never have to tap a field
 * first). Manual entry works through the on-screen number pad. A rejected scan
 * shakes the entry well and renders the backend detail VERBATIM under it.
 *
 * Badge = identity: one operator per login, no shared accounts.
 */
export default function KioskBadgeLogin({ stationLabel, onLogin }: KioskBadgeLoginProps) {
  const [value, setValue] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Re-keys the well so the shake replays on every consecutive rejection.
  const [shakeKey, setShakeKey] = useState(0);
  const [nowMs, setNowMs] = useState(() => Date.now());
  // Browser connectivity — the only honest signal pre-login (no poll yet).
  const [browserOnline, setBrowserOnline] = useState(() =>
    typeof navigator === 'undefined' ? true : navigator.onLine
  );

  useEffect(() => {
    const interval = window.setInterval(() => setNowMs(Date.now()), 1000);
    const handleOnline = () => setBrowserOnline(true);
    const handleOffline = () => setBrowserOnline(false);
    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
  }, []);

  const submit = useCallback(
    async (raw: string) => {
      const employeeId = raw.trim();
      if (!employeeId || submitting) return;
      setSubmitting(true);
      setError(null);
      try {
        await onLogin(employeeId);
        setValue('');
      } catch (err: unknown) {
        const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
        const message = (err as { message?: unknown })?.message;
        setError(
          (typeof detail === 'string' && detail) || (typeof message === 'string' && message) || 'Login failed. Try again.'
        );
        setShakeKey((k) => k + 1);
        setValue(''); // clear for a clean re-scan
      } finally {
        setSubmitting(false);
      }
    },
    [onLogin, submitting]
  );

  // Window-level capture: badge scanners type wherever "focus" happens to be.
  useBadgeCapture({
    enabled: !submitting,
    value,
    onValueChange: setValue,
    onSubmit: (raw) => void submit(raw),
  });

  return (
    <div className="fd-scope-kiosk flex min-h-screen flex-col bg-fd-canvas [background-image:linear-gradient(rgba(36,48,68,0.18)_1px,transparent_1px),linear-gradient(90deg,rgba(36,48,68,0.18)_1px,transparent_1px)] [background-size:28px_28px]">
      {/* Top bar — logo in the operator-chip slot's place (1a variant) */}
      <header className="flex h-[60px] shrink-0 items-center gap-3.5 border-b border-fd-line bg-fd-panel px-6">
        <img src="/Werco_Logo_white.png" alt="Werco" className="h-[22px] w-auto" />
        <div className="h-6 w-px bg-fd-line" aria-hidden="true" />
        <span className="font-mono text-sm font-bold uppercase tracking-[0.04em] text-fd-ink">{stationLabel}</span>
        <div className="flex-1" />
        <span
          className={`inline-flex items-center gap-1.5 rounded-[3px] border px-2 py-1 font-mono text-[11px] font-semibold uppercase tracking-[0.08em] ${
            browserOnline ? 'border-fd-green/40 bg-fd-green/10 text-fd-green' : 'border-fd-red/50 bg-fd-red/10 text-fd-red'
          }`}
        >
          <span
            aria-hidden="true"
            className={`h-1.5 w-1.5 rounded-full ${
              browserOnline ? 'bg-fd-green shadow-[0_0_6px_var(--fd-green)]' : 'bg-fd-red'
            }`}
          />
          {browserOnline ? 'Online' : 'Offline'}
        </span>
        <span className="font-mono text-[13px] tabular-nums text-fd-ink">
          {formatCentralTime(nowMs, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}
        </span>
      </header>

      {/* Centered two-column layout (stacks in portrait) */}
      <main className="flex flex-1 flex-col items-center justify-center gap-10 px-6 py-10 min-[1100px]:flex-row min-[1100px]:gap-16">
        <div className="flex w-full max-w-[380px] flex-col gap-5">
          <div>
            <p className="font-mono text-[11px] uppercase tracking-[0.2em] text-fd-blue">Operator sign-in</p>
            <h1 className="mt-2.5 text-3xl font-bold leading-tight tracking-[-0.02em] text-fd-ink">
              Scan badge or enter ID
            </h1>
            <p className="mt-2.5 text-sm leading-relaxed text-fd-body">
              Hold your badge to the reader below the screen, or key in your badge ID.
            </p>
          </div>

          <div
            key={shakeKey}
            data-testid="kiosk-badge-display"
            className={`flex h-[76px] items-center justify-center gap-3.5 rounded-[4px] border bg-fd-sunken px-4 ${
              error ? 'animate-kiosk-shake border-fd-red' : 'border-fd-line-bright'
            }`}
          >
            {value ? (
              <span className="max-w-full truncate font-mono text-[34px] font-bold tracking-[0.2em] text-fd-ink">
                {value}
              </span>
            ) : (
              <span className="font-mono text-lg uppercase tracking-[0.14em] text-fd-faint">Waiting for badge…</span>
            )}
            <span
              aria-hidden="true"
              className="animate-kiosk-caret h-9 w-0.5 shrink-0 bg-fd-blue shadow-[0_0_8px_rgba(47,129,247,0.5)]"
            />
          </div>

          {error && (
            <div
              role="alert"
              className="w-full rounded-[4px] border border-fd-red bg-fd-red/10 px-4 py-3 text-center text-lg font-semibold text-fd-red"
            >
              {error}
            </div>
          )}

          <div className="flex items-center gap-2.5 rounded-[4px] border border-fd-blue/25 bg-fd-blue/5 px-3.5 py-3">
            <span
              aria-hidden="true"
              className="h-2 w-2 animate-pulse rounded-full bg-fd-blue shadow-[0_0_8px_rgba(47,129,247,0.6)]"
            />
            <span className="font-mono text-[11px] uppercase tracking-[0.1em] text-fd-body">
              Badge reader active · listening
            </span>
          </div>

          <p className="mt-2 flex justify-center gap-2.5 font-mono text-[10px] uppercase tracking-[0.16em] text-fd-faint">
            <span>AS9100D</span>
            <span aria-hidden="true">·</span>
            <span>ISO 9001</span>
            <span aria-hidden="true">·</span>
            <span>ITAR</span>
            <span aria-hidden="true">·</span>
            <span>CMMC L2</span>
          </p>
        </div>

        <div className="flex w-full max-w-[340px] flex-col gap-2.5">
          <KioskKeypad
            value={value}
            onChange={setValue}
            maxLength={MAX_BADGE_LENGTH}
            disabled={submitting}
            idPrefix="kiosk-badge-key"
            size="lg"
          />
          <button
            type="button"
            onClick={() => void submit(value)}
            disabled={submitting || !value.trim()}
            className="h-16 w-full rounded-[4px] bg-fd-blue font-mono text-base font-bold uppercase tracking-[0.1em] text-[#04101f] transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
          >
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
          <p className="mt-2 text-center text-xs text-fd-mute">
            Your badge is your identity — work you record here is recorded in your name.
          </p>
        </div>
      </main>
    </div>
  );
}
