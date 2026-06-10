import React, { useCallback, useEffect, useState } from 'react';
import { IdentificationIcon, ArrowRightCircleIcon } from '@heroicons/react/24/solid';
import KioskKeypad from './KioskKeypad';

interface KioskBadgeLoginProps {
  stationLabel: string;
  onLogin: (employeeId: string) => Promise<void>;
}

const MAX_BADGE_LENGTH = 32;
// Printable characters a badge/wedge scanner can emit (employee IDs like "EMP-0042").
const BADGE_CHAR = /^[0-9A-Za-z\-_.]$/;

/**
 * Full-screen badge prompt. A keyboard-wedge scanner "types" the employee id
 * and sends Enter — captured at the window level so it works without any
 * focused input (gloved operators never have to tap a field first). Manual
 * entry works through the on-screen number pad.
 *
 * Badge = identity: one operator per login, no shared accounts.
 */
export default function KioskBadgeLogin({ stationLabel, onLogin }: KioskBadgeLoginProps) {
  const [value, setValue] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
        setValue(''); // clear for a clean re-scan
      } finally {
        setSubmitting(false);
      }
    },
    [onLogin, submitting]
  );

  // Window-level capture: badge scanners type wherever "focus" happens to be.
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (submitting) return;
      if (event.key === 'Enter') {
        event.preventDefault();
        void submit(value);
        return;
      }
      if (event.key === 'Backspace') {
        setValue((prev) => prev.slice(0, -1));
        return;
      }
      if (BADGE_CHAR.test(event.key)) {
        setValue((prev) => (prev.length >= MAX_BADGE_LENGTH ? prev : prev + event.key));
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [submit, submitting, value]);

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-fd-canvas px-6 py-10">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <p className="font-mono text-sm uppercase tracking-[0.3em] text-fd-mute">{stationLabel}</p>
          <h1 className="mt-3 text-4xl font-bold text-fd-ink">Scan your badge</h1>
          <p className="mt-2 text-lg text-fd-body">or enter your employee ID below</p>
        </div>

        <div
          data-testid="kiosk-badge-display"
          className="mb-4 flex min-h-20 items-center justify-center rounded border border-fd-line-bright bg-fd-sunken px-4"
        >
          {value ? (
            <span className="font-mono text-4xl font-semibold tracking-[0.2em] text-fd-ink">{value}</span>
          ) : (
            <span className="flex items-center gap-3 text-fd-faint">
              <IdentificationIcon className="h-8 w-8" />
              <span className="text-xl">Waiting for badge…</span>
            </span>
          )}
        </div>

        {error && (
          <div
            role="alert"
            className="mb-4 w-full rounded border border-fd-red bg-fd-red/10 px-4 py-4 text-center text-xl font-semibold text-fd-red"
          >
            {error}
          </div>
        )}

        <KioskKeypad value={value} onChange={setValue} maxLength={MAX_BADGE_LENGTH} disabled={submitting} idPrefix="kiosk-badge-key" />

        <button
          type="button"
          onClick={() => void submit(value)}
          disabled={submitting || !value.trim()}
          className="mt-4 flex min-h-20 w-full items-center justify-center gap-3 rounded border border-werco-navy-600 bg-werco-navy-600 text-2xl font-bold uppercase tracking-wider text-white transition-colors hover:bg-werco-navy-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <ArrowRightCircleIcon className="h-9 w-9" />
          {submitting ? 'Logging in…' : 'Log in'}
        </button>

        <p className="mt-6 text-center text-sm text-fd-faint">
          Your badge is your identity — work you record here is recorded in your name.
        </p>
      </div>
    </div>
  );
}
