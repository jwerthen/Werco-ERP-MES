import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation } from 'react-router-dom';
import {
  ArrowLeftCircleIcon,
  ArrowRightOnRectangleIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  SignalIcon,
  SignalSlashIcon,
} from '@heroicons/react/24/solid';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';
import { ActiveJob } from '../types';
import {
  getKioskIdleLogoutSeconds,
  getKioskWorkCenterCode,
  getKioskWorkCenterId,
} from '../utils/kiosk';
import { useKioskIdleLogout } from '../hooks/useKioskIdleLogout';
import KioskBadgeLogin from '../components/kiosk/KioskBadgeLogin';
import KioskActiveJobBanner from '../components/kiosk/KioskActiveJobBanner';
import KioskQueueCard from '../components/kiosk/KioskQueueCard';
import KioskQuantityScreen from '../components/kiosk/KioskQuantityScreen';
import KioskReasonGrid from '../components/kiosk/KioskReasonGrid';
import LaserNestOperatorPanel from '../components/laser/LaserNestOperatorPanel';
import { HOLD_REASONS, KIOSK_SOURCE, KioskQueueItem, kioskErrorMessage } from '../components/kiosk/kioskConstants';

const POLL_INTERVAL_MS = 15_000;

type KioskView =
  | { name: 'queue' }
  | { name: 'confirm'; item: KioskQueueItem }
  | { name: 'production'; job: ActiveJob }
  | { name: 'complete'; job: ActiveJob }
  | { name: 'hold'; job: ActiveJob };

interface KioskToast {
  id: number;
  type: 'success' | 'error' | 'info';
  message: string;
}

let toastSeq = 0;

function jobLabel(job: ActiveJob): string {
  return `${job.work_order_number || '—'} · Op ${job.operation_number ?? '—'} ${job.operation_name || ''}`.trim();
}

/**
 * A0.3 Operator kiosk — touch-first station screen at /kiosk.
 *
 * Deliberately tiny: badge login → station queue → tap a job → CLOCK IN, plus
 * a pinned active-job banner with REPORT PRODUCTION / COMPLETE / HOLD.
 * Every mutation reports source:"kiosk" (A0.1 adoption telemetry). Backend
 * gating errors (sequence/predecessor/hold) are surfaced VERBATIM, never
 * suppressed. No supervisor verbs live here.
 */
export default function OperatorKiosk() {
  const location = useLocation();
  const { user, isAuthenticated, isLoading, loginWithEmployeeId, logout } = useAuth();

  const workCenterId = getKioskWorkCenterId(location.search);
  const workCenterCode = getKioskWorkCenterCode(location.search);
  const idleLogoutSeconds = getKioskIdleLogoutSeconds(location.search);

  const [queue, setQueue] = useState<KioskQueueItem[]>([]);
  const [activeJob, setActiveJob] = useState<ActiveJob | null>(null);
  const [workCenterName, setWorkCenterName] = useState<string | null>(null);
  const [online, setOnline] = useState(true);
  const [initialLoadDone, setInitialLoadDone] = useState(false);
  const [view, setView] = useState<KioskView>({ name: 'queue' });
  const [busy, setBusy] = useState(false);
  const [holdReason, setHoldReason] = useState<string | null>(null);
  const [toasts, setToasts] = useState<KioskToast[]>([]);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const showToast = useCallback((type: KioskToast['type'], message: string) => {
    const id = ++toastSeq;
    setToasts((prev) => [...prev.slice(-2), { id, type, message }]);
    // Errors must stay readable from arm's length: linger 4x longer.
    window.setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), type === 'error' ? 12_000 : 3_000);
  }, []);

  // --- Data refresh (15s poll; WebSocket can come later) -------------------
  const refresh = useCallback(async () => {
    if (!isAuthenticated || workCenterId == null) return;
    try {
      const [queueRes, activeRes] = await Promise.all([api.getWorkCenterQueue(workCenterId), api.getMyActiveJob()]);
      setQueue(queueRes.queue || []);
      const jobs: ActiveJob[] = activeRes.active_jobs || (activeRes.active_job ? [activeRes.active_job] : []);
      setActiveJob(jobs[0] || null);
      setOnline(true);
    } catch {
      // Transient failure: keep last-known data and all form state; show OFFLINE.
      setOnline(false);
    } finally {
      setInitialLoadDone(true);
    }
  }, [isAuthenticated, workCenterId]);

  useEffect(() => {
    if (!isAuthenticated || workCenterId == null) return undefined;
    void refresh();
    const interval = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [isAuthenticated, workCenterId, refresh]);

  // Resolve the station's display name once per login (best effort).
  useEffect(() => {
    if (!isAuthenticated || workCenterId == null) return;
    let cancelled = false;
    api
      .getWorkCenters()
      .then((centers) => {
        if (cancelled) return;
        const match = (centers || []).find((wc) => wc.id === workCenterId);
        if (match) setWorkCenterName(`${match.code} · ${match.name}`);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, workCenterId]);

  // 1s ticker for the running-job timer.
  useEffect(() => {
    if (!activeJob) return undefined;
    const interval = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [activeJob]);

  // --- Idle auto-logout -----------------------------------------------------
  const handleIdleLogout = useCallback(() => {
    logout();
    setView({ name: 'queue' });
    setQueue([]);
    setActiveJob(null);
  }, [logout]);

  const { countdownSeconds } = useKioskIdleLogout({
    enabled: isAuthenticated,
    timeoutSeconds: idleLogoutSeconds,
    onTimeout: handleIdleLogout,
  });

  // Mutations are blocked while a request is in flight (busy) OR while the
  // station is offline. Firing a clock-in/out, complete, hold, or scrap against
  // a dead connection silently drops the record, so we hard-disable the buttons
  // rather than letting the tap no-op. The offline banner (id below) carries the
  // human-readable reason and is referenced via aria-describedby.
  const mutationsBlocked = busy || !online;
  const OFFLINE_HINT_ID = 'kiosk-offline-hint';

  // --- Mutations (all send source:"kiosk") ----------------------------------
  const handleClockIn = useCallback(
    async (item: KioskQueueItem) => {
      if (workCenterId == null) return;
      setBusy(true);
      try {
        await api.clockIn({
          work_order_id: item.work_order_id,
          operation_id: item.operation_id,
          work_center_id: workCenterId,
          entry_type: 'run',
          source: KIOSK_SOURCE,
        });
        showToast('success', `Clocked in to ${item.work_order_number}`);
        setView({ name: 'queue' });
        await refresh();
      } catch (err) {
        showToast('error', kioskErrorMessage(err, 'Could not clock in. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [workCenterId, refresh, showToast]
  );

  const handleReportProduction = useCallback(
    async (job: ActiveJob, good: number, scrap: number, scrapReason: string | null) => {
      if (!job.operation_id) return;
      setBusy(true);
      try {
        await api.reportOperationProduction(job.operation_id, {
          quantity_complete_delta: good,
          quantity_scrapped_delta: scrap,
          // Structured scrap reason (same TimeEntry.scrap_reason column clock-out
          // writes). The kiosk has no free-text notes input, so `notes` is not sent.
          scrap_reason: scrap > 0 && scrapReason ? scrapReason : undefined,
          source: KIOSK_SOURCE,
        });
        showToast('success', scrap > 0 ? `Saved ${good} good, ${scrap} scrap` : `Saved ${good} good`);
        setView({ name: 'queue' });
        await refresh();
      } catch (err) {
        // Keep the production view (and its entered quantities) on failure.
        showToast('error', kioskErrorMessage(err, 'Could not save production. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [refresh, showToast]
  );

  const handleComplete = useCallback(
    async (job: ActiveJob, good: number, scrap: number, scrapReason: string | null) => {
      if (!job.operation_id) return;
      setBusy(true);
      let clockedOut = false;
      try {
        // Mirrors ShopFloorSimple's sequencing: close the operator's own labor
        // record first (quantities + scrap reason land on the TimeEntry), then
        // assert operation completion at the target quantity.
        await api.clockOut(job.time_entry_id, {
          quantity_produced: good,
          quantity_scrapped: scrap,
          scrap_reason: scrap > 0 && scrapReason ? scrapReason : undefined,
          source: KIOSK_SOURCE,
        });
        clockedOut = true;
        await api.completeOperation(job.operation_id, {
          quantity_complete: Number(job.quantity_ordered || 0),
          source: KIOSK_SOURCE,
        });
        showToast('success', `Completed ${job.work_order_number}`);
        setView({ name: 'queue' });
      } catch (err) {
        // Two-step verb: if the clock-out landed but completion was refused, say so
        // honestly AND keep the backend's gating detail verbatim.
        if (clockedOut) {
          showToast('error', `Clocked out, but completing failed: ${kioskErrorMessage(err, 'Could not complete. Try again.')}`);
          setView({ name: 'queue' });
        } else {
          showToast('error', kioskErrorMessage(err, 'Could not complete. Try again.'));
        }
      } finally {
        setBusy(false);
        await refresh();
      }
    },
    [refresh, showToast]
  );

  const handleHold = useCallback(
    async (job: ActiveJob, category: string) => {
      if (!job.operation_id) return;
      setBusy(true);
      try {
        await api.holdOperation(job.operation_id, {
          category,
          severity: 'medium',
          // The backend only files a WorkOrderBlocker when the hold carries a note
          // OR a non-OTHER category; the kiosk's "Other" tile is category-only, so
          // send a stub note to make sure every kiosk hold files a blocker.
          ...(category === 'other' ? { note: 'Other (reported at kiosk)' } : {}),
          source: KIOSK_SOURCE,
        });
        showToast('info', 'Operation placed on hold');
        setView({ name: 'queue' });
        setHoldReason(null);
        await refresh();
      } catch (err) {
        showToast('error', kioskErrorMessage(err, 'Could not place on hold. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [refresh, showToast]
  );

  // --- Station identity guards ----------------------------------------------
  const stationLabel = useMemo(
    () => workCenterName || workCenterCode || (workCenterId != null ? `Work center #${workCenterId}` : 'Station'),
    [workCenterName, workCenterCode, workCenterId]
  );

  if (workCenterId == null) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center bg-fd-canvas px-6 text-center">
        <ExclamationTriangleIcon className="h-16 w-16 text-fd-amber" />
        <h1 className="mt-4 text-3xl font-bold text-fd-ink">Station not configured</h1>
        <p className="mt-3 max-w-xl text-lg text-fd-body">
          Open this kiosk with a station URL, e.g.{' '}
          <code className="rounded bg-fd-sunken px-2 py-1 font-mono text-fd-cyan">
            /kiosk?kiosk=1&amp;work_center_id=12&amp;work_center_code=LASER1
          </code>
        </p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-fd-canvas">
        <p className="text-2xl text-fd-mute">Starting kiosk…</p>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <KioskBadgeLogin stationLabel={stationLabel} onLogin={loginWithEmployeeId} />;
  }

  // --- Authenticated kiosk ----------------------------------------------------
  return (
    <div className="flex min-h-screen flex-col bg-fd-canvas">
      {/* Station header — always visible */}
      <header className="sticky top-0 z-30 border-b border-fd-line bg-fd-panel px-5 py-3">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-4">
          <div className="min-w-0">
            <p className="truncate font-mono text-xl font-bold tracking-tight text-fd-ink">{stationLabel}</p>
            <p className="truncate text-sm text-fd-mute">
              {user?.first_name} {user?.last_name} · {user?.employee_id || '—'}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <span
              data-testid="kiosk-connection"
              className={`flex items-center gap-2 rounded border px-3 py-2 font-mono text-sm font-bold uppercase tracking-widest ${
                online ? 'border-fd-green/50 text-fd-green' : 'border-fd-red bg-fd-red/10 text-fd-red'
              }`}
            >
              {online ? <SignalIcon className="h-5 w-5" /> : <SignalSlashIcon className="h-5 w-5" />}
              {online ? 'Online' : 'Offline'}
            </span>
            <button
              type="button"
              onClick={handleIdleLogout}
              // Disabled mid-mutation: logging out while a clock-in/out is in flight
              // 401s the retry path and bounces the tablet off /kiosk.
              disabled={busy}
              className="flex min-h-16 items-center gap-2 rounded border border-fd-line bg-fd-sunken px-4 text-lg font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:cursor-not-allowed disabled:opacity-40"
            >
              <ArrowRightOnRectangleIcon className="h-6 w-6" />
              Log out
            </button>
          </div>
        </div>
      </header>

      {/* Offline banner — also the accessible explanation for the disabled
          mutation buttons (referenced via aria-describedby). */}
      {!online && (
        <div
          role="alert"
          id={OFFLINE_HINT_ID}
          className="border-b border-fd-red bg-fd-red/15 px-5 py-4 text-center text-xl font-bold text-fd-red"
        >
          OFFLINE — actions are disabled until the connection is restored. Reconnecting…
        </div>
      )}

      {/* Idle-logout countdown toast */}
      {countdownSeconds != null && (
        <div
          role="alert"
          data-testid="kiosk-idle-countdown"
          className="border-b border-fd-amber bg-fd-amber/15 px-5 py-4 text-center text-xl font-bold text-fd-amber"
        >
          Logging out in {countdownSeconds}s — tap anywhere to stay logged in
        </div>
      )}

      {/* Toasts — full width, plain language */}
      <div className="fixed inset-x-0 bottom-0 z-40 space-y-2 p-3">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role={toast.type === 'error' ? 'alert' : 'status'}
            className={`flex w-full items-center gap-3 rounded border px-5 py-4 text-xl font-semibold shadow-lg ${
              toast.type === 'success'
                ? 'border-fd-green bg-[#0c2415] text-fd-green'
                : toast.type === 'error'
                  ? 'border-fd-red bg-[#2a0f0c] text-fd-red'
                  : 'border-fd-blue bg-[#0c1a2e] text-fd-blue'
            }`}
          >
            {toast.type === 'success' ? (
              <CheckCircleIcon className="h-7 w-7 shrink-0" />
            ) : (
              <ExclamationTriangleIcon className="h-7 w-7 shrink-0" />
            )}
            <span>{toast.message}</span>
          </div>
        ))}
      </div>

      <main className="mx-auto w-full max-w-5xl flex-1 space-y-5 px-4 py-5">
        {view.name === 'queue' && (
          <>
            {activeJob && (
              <KioskActiveJobBanner
                job={activeJob}
                nowMs={nowMs}
                busy={mutationsBlocked}
                onReportProduction={() => setView({ name: 'production', job: activeJob })}
                onComplete={() => setView({ name: 'complete', job: activeJob })}
                onHold={() => {
                  setHoldReason(null);
                  setView({ name: 'hold', job: activeJob });
                }}
              />
            )}

            <section aria-label="Work queue">
              <h2 className="mb-3 font-mono text-sm font-bold uppercase tracking-[0.25em] text-fd-mute">
                My queue · {queue.length} job{queue.length === 1 ? '' : 's'}
              </h2>
              {!initialLoadDone ? (
                <p className="py-10 text-center text-xl text-fd-mute">Loading queue…</p>
              ) : queue.length === 0 ? (
                <p className="rounded border border-fd-line bg-fd-panel py-10 text-center text-xl text-fd-mute">
                  No jobs in this station&apos;s queue.
                </p>
              ) : (
                <div className="space-y-3">
                  {queue.map((item) => (
                    <KioskQueueCard key={item.operation_id} item={item} disabled={mutationsBlocked} onSelect={(it) => setView({ name: 'confirm', item: it })} />
                  ))}
                </div>
              )}
            </section>
          </>
        )}

        {view.name === 'confirm' && (
          <section aria-label="Confirm clock in" className="mx-auto w-full max-w-2xl">
            <h2 className="text-3xl font-bold text-fd-ink">Clock in?</h2>
            <div className="mt-4 rounded border border-fd-line bg-fd-panel p-6">
              <p className="font-mono text-4xl font-bold text-fd-ink">{view.item.work_order_number}</p>
              <p className="mt-3 text-2xl text-fd-body">
                <span className="font-mono font-semibold text-fd-ink">{view.item.part_number || '—'}</span>
                {view.item.part_name ? <span className="text-fd-mute"> · {view.item.part_name}</span> : null}
              </p>
              <p className="mt-1 text-xl text-fd-mute">
                Op {view.item.operation_number ?? '—'} · {view.item.operation_name || 'Operation'}
              </p>
              <p className="mt-3 font-mono text-2xl text-fd-body">
                {Number(view.item.quantity_complete || 0)} / {Number(view.item.quantity_ordered || 0)} pcs
              </p>
              {view.item.laser_nest && (
                <div className="mt-4">
                  <LaserNestOperatorPanel nest={view.item.laser_nest} size="kiosk" />
                </div>
              )}
            </div>
            <div className="mt-5 grid grid-cols-2 gap-3">
              <button
                type="button"
                disabled={busy}
                onClick={() => setView({ name: 'queue' })}
                className="flex min-h-20 items-center justify-center gap-2 rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
              >
                <ArrowLeftCircleIcon className="h-7 w-7" />
                Back
              </button>
              <button
                type="button"
                disabled={mutationsBlocked}
                aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                title={!online ? 'Offline — clock in is disabled until reconnected' : undefined}
                onClick={() => void handleClockIn(view.item)}
                className="min-h-20 rounded border border-fd-green bg-fd-green/15 text-2xl font-bold uppercase tracking-wide text-fd-green transition-colors hover:bg-fd-green/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {!online ? 'Offline' : busy ? 'Clocking in…' : 'Clock in'}
              </button>
            </div>
          </section>
        )}

        {view.name === 'production' && (
          <KioskQuantityScreen
            title="Report production"
            jobLabel={jobLabel(view.job)}
            confirmLabel="Save"
            requireTotalPositive
            busy={mutationsBlocked}
            onConfirm={(good, scrap, reason) => void handleReportProduction(view.job, good, scrap, reason)}
            onCancel={() => setView({ name: 'queue' })}
          />
        )}

        {view.name === 'complete' && (
          <KioskQuantityScreen
            title="Complete job"
            jobLabel={jobLabel(view.job)}
            confirmLabel="Complete"
            initialGood={Math.max(0, Number(view.job.quantity_ordered || 0) - Number(view.job.quantity_complete || 0))}
            requireTotalPositive={false}
            busy={mutationsBlocked}
            onConfirm={(good, scrap, reason) => void handleComplete(view.job, good, scrap, reason)}
            onCancel={() => setView({ name: 'queue' })}
          />
        )}

        {view.name === 'hold' && (
          <section aria-label="Hold job" className="mx-auto w-full max-w-2xl">
            <h2 className="text-3xl font-bold text-fd-ink">Hold job</h2>
            <p className="mt-1 font-mono text-lg text-fd-mute">{jobLabel(view.job)}</p>
            <p className="mt-4 mb-2 text-lg font-semibold text-fd-amber">Why is this job stopping? — required</p>
            <KioskReasonGrid reasons={HOLD_REASONS} selected={holdReason} onSelect={setHoldReason} disabled={mutationsBlocked} tone="amber" />
            <div className="mt-6 grid grid-cols-2 gap-3">
              <button
                type="button"
                disabled={busy}
                onClick={() => setView({ name: 'queue' })}
                className="min-h-20 rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={mutationsBlocked || !holdReason}
                aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                title={!online ? 'Offline — hold is disabled until reconnected' : undefined}
                onClick={() => holdReason && void handleHold(view.job, holdReason)}
                className="min-h-20 rounded border border-fd-amber bg-fd-amber/15 text-xl font-bold uppercase tracking-wide text-fd-amber transition-colors hover:bg-fd-amber/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {!online ? 'Offline' : busy ? 'Holding…' : 'Hold job'}
              </button>
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
