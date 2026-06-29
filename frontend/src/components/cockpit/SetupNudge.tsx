import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { RocketLaunchIcon, XMarkIcon, ArrowRightIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { usePermissions } from '../../hooks/usePermissions';

const DISMISS_KEY = 'werco-setup-nudge-dismissed';

/**
 * Dismissible "Finish setup" nudge for admins. Surfaces the buried Setup
 * Wizard (/setup) on the Dashboard with a live progress signal pulled from
 * the setup-health endpoint. RBAC-gated to admins; hidden once setup is
 * complete (100%) or the user dismisses it (persisted in localStorage).
 *
 * Instrument-panel styled — hairline border, sharp corners, dense.
 */
export default function SetupNudge() {
  const { isAdmin } = usePermissions();
  const [progress, setProgress] = useState<number | null>(null);
  const [dismissed, setDismissed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(DISMISS_KEY) === '1';
    } catch {
      return false;
    }
  });

  useEffect(() => {
    if (!isAdmin || dismissed) return;
    let cancelled = false;
    (async () => {
      try {
        const health = await api.getSetupHealth();
        if (!cancelled) {
          setProgress(typeof health?.progress === 'number' ? health.progress : null);
        }
      } catch {
        // Setup health is best-effort; if it fails, fall back to a generic
        // "complete your setup" prompt (progress stays null).
        if (!cancelled) setProgress(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isAdmin, dismissed]);

  const handleDismiss = () => {
    setDismissed(true);
    try {
      localStorage.setItem(DISMISS_KEY, '1');
    } catch {
      /* non-fatal */
    }
  };

  // Gate: admins only, not dismissed, and not already 100% complete.
  if (!isAdmin || dismissed || progress === 100) return null;

  const label = progress !== null ? `Finish setup — ${progress}% complete` : 'Complete your setup';

  return (
    <div className="flex items-center gap-3 rounded-sm border border-fd-blue/40 bg-fd-blue/10 px-3 py-2">
      <RocketLaunchIcon className="h-5 w-5 flex-shrink-0 text-fd-blue" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-semibold text-fd-ink truncate">{label}</p>
        <p className="text-xs text-fd-mute truncate">
          Load the master data needed to run your first clean job.
        </p>
      </div>
      {progress !== null && (
        <div className="hidden sm:block w-28 flex-shrink-0">
          <div className="h-1.5 rounded-full bg-slate-800 overflow-hidden">
            <div
              className="h-full rounded-full bg-fd-blue transition-all"
              style={{ width: `${progress}%` }}
            />
          </div>
        </div>
      )}
      <Link
        to="/setup"
        className="inline-flex flex-shrink-0 items-center gap-1.5 rounded-sm bg-fd-blue px-2.5 py-1 text-xs font-semibold text-white transition-colors hover:bg-fd-blue/90"
      >
        Finish setup
        <ArrowRightIcon className="h-3.5 w-3.5" aria-hidden="true" />
      </Link>
      <button
        type="button"
        onClick={handleDismiss}
        className="flex-shrink-0 rounded-sm p-1 text-fd-mute transition-colors hover:bg-white/5 hover:text-fd-ink"
        aria-label="Dismiss setup reminder"
        title="Dismiss"
      >
        <XMarkIcon className="h-4 w-4" />
      </button>
    </div>
  );
}
