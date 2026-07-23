import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation } from 'react-router-dom';
import {
  ArrowLeftIcon,
  ExclamationTriangleIcon,
  PlusIcon,
} from '@heroicons/react/24/outline';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';
import { ActiveJob, KioskQueueWorkCenter, LaserNestInfo, MyActiveJobResponse } from '../types';
import {
  getKioskIdleLogoutSeconds,
  getKioskWorkCenterCode,
  getKioskWorkCenterId,
} from '../utils/kiosk';
import { formatCentralTime } from '../utils/centralTime';
import { useKioskIdleLogout } from '../hooks/useKioskIdleLogout';
import KioskBadgeLogin from '../components/kiosk/KioskBadgeLogin';
import KioskNcrFiledScreen from '../components/kiosk/KioskNcrFiledScreen';
import KioskQueueCard from '../components/kiosk/KioskQueueCard';
import KioskCorrectionScreen from '../components/kiosk/KioskCorrectionScreen';
import KioskReportModal from '../components/kiosk/KioskReportModal';
import KioskHoldModal from '../components/kiosk/KioskHoldModal';
import KioskCompleteModal from '../components/kiosk/KioskCompleteModal';
import KioskDocViewer, { KioskDocTab, KioskDocTransport } from '../components/kiosk/KioskDocViewer';
import KioskStepsPanel, { StepsTransport } from '../components/kiosk/KioskStepsPanel';
import LaserNestOperatorPanel from '../components/laser/LaserNestOperatorPanel';
import {
  KIOSK_SOURCE,
  KioskQueueItem,
  KioskWorkCenterQueueResponse,
  formatElapsed,
  formatStepsChip,
  kioskErrorMessage,
} from '../components/kiosk/kioskConstants';
import {
  clockedOutStepsMessage,
  extractClockOutStepsIncomplete,
  extractStepsIncomplete,
  stepsIncompleteMessage,
} from '../utils/processSheetErrors';
import type { MissingStepInfo, QualityHoldResult } from '../types/processSheet';
import { useScrapReasonCodes } from '../hooks/useScrapReasonCodes';

const POLL_INTERVAL_MS = 15_000;

type KioskView =
  | { name: 'queue' }
  | { name: 'confirm'; item: KioskQueueItem }
  | { name: 'production'; job: ActiveJob; tab: 'good' | 'scrap' }
  // Over-count correction (reduce-production) — walk back good pieces the
  // operator OVER-reported on their own open clock-in. Server-gated, non-optimistic.
  | { name: 'correct'; job: ActiveJob }
  | { name: 'complete'; job: ActiveJob }
  | { name: 'hold'; job: ActiveJob }
  | { name: 'steps'; operationId: number; jobLabel: string; missing?: MissingStepInfo[] | null }
  // Full-screen drawing / nest viewer (Foundry 1h).
  | { name: 'viewer'; operationId: number; initialTab: KioskDocTab }
  // One-tap OOT hold succeeded: the NCR number must stay readable (queue
  // refreshes must not yank it), so it gets its own view like every other verb.
  | { name: 'ncrFiled'; result: QualityHoldResult; jobLabel: string };

/**
 * The logged-in operator's session drives the main api client directly. Every
 * WRITE reports source:"kiosk" — the same A0.1 adoption-telemetry channel
 * clock-in sends (this kiosk uses a normal session, so unlike the crew
 * station's badge tokens the server has no credential to derive it from).
 */
const OPERATOR_STEPS_TRANSPORT: StepsTransport = {
  fetchView: (operationId) => api.getOperationSteps(operationId),
  createRecord: (operationId, stepId, data) =>
    api.recordOperationStep(operationId, stepId, { ...data, source: KIOSK_SOURCE }),
  supersedeRecord: (operationId, stepId, recordId, data) =>
    api.supersedeOperationStepRecord(operationId, stepId, recordId, { ...data, source: KIOSK_SOURCE }),
  uploadAttachment: (operationId, stepId, file) => api.uploadOperationStepAttachment(operationId, stepId, file),
  qualityHold: (operationId, stepId, data) =>
    api.raiseStepQualityHold(operationId, stepId, { ...data, source: KIOSK_SOURCE }),
};

/** Doc-viewer transport: session-authed, shop-floor-fenced reads. Never navigates. */
const OPERATOR_DOC_TRANSPORT: KioskDocTransport = {
  fetchOperationDocuments: (operationId) => api.getOperationDocuments(operationId),
  fetchDocumentBlob: (documentId) => api.fetchShopFloorDocumentBlob(documentId),
};

/**
 * Fence-safe nest-PDF fetcher for LaserNestOperatorPanel on THIS kiosk: the
 * shop-floor inline route via the session client (the /kiosk interceptor guard
 * rejects instead of navigating on a dead session — previewing a nest must
 * never bounce the station to /login).
 */
function operatorNestPdfFetcher(nest: LaserNestInfo | null | undefined): (() => Promise<string>) | undefined {
  if (!nest || nest.document_id == null) return undefined;
  const documentId = nest.document_id;
  return () => api.fetchShopFloorDocumentBlob(documentId);
}

interface KioskToast {
  id: number;
  type: 'success' | 'error' | 'info';
  message: string;
}

let toastSeq = 0;

function jobLabel(job: ActiveJob): string {
  return `${job.work_order_number || '—'} · Op ${job.operation_number ?? '—'} ${job.operation_name || ''}`.trim();
}

/** "00:05" — blocker downtime as H:MM/HH:MM from minutes. */
function formatDowntime(minutes: number): string {
  const total = Math.max(0, Math.round(minutes));
  const h = Math.floor(total / 60);
  const m = total % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

/** "41s" / "2m 05s" — average seconds per piece. */
function formatAvgPerPc(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${String(s).padStart(2, '0')}s`;
}

const CLOCK_OPTIONS: Intl.DateTimeFormatOptions = {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
};

const TIME_HM_OPTIONS: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit', hour12: false };

/**
 * A0.3 Operator kiosk — touch-first station screen at /kiosk, in the Foundry
 * redesign chrome (design handoff 1a/1b/1c–1g/1i + the 1h doc viewer).
 *
 * Badge login → running job + queue split → report/hold/complete overlays.
 * Every mutation reports source:"kiosk" (A0.1 adoption telemetry). Backend
 * gating errors (sequence/predecessor/hold) are surfaced VERBATIM, never
 * suppressed, and every verb is server-gated ⇒ NON-optimistic. No supervisor
 * verbs live here.
 */
export default function OperatorKiosk() {
  const location = useLocation();
  const { user, isAuthenticated, isLoading, loginWithEmployeeId, logout } = useAuth();

  const workCenterId = getKioskWorkCenterId(location.search);
  const workCenterCode = getKioskWorkCenterCode(location.search);
  const idleLogoutSeconds = getKioskIdleLogoutSeconds(location.search);

  const [queue, setQueue] = useState<KioskQueueItem[]>([]);
  const [activeJob, setActiveJob] = useState<ActiveJob | null>(null);
  const [workCenter, setWorkCenter] = useState<KioskQueueWorkCenter | null>(null);
  const [workCenterName, setWorkCenterName] = useState<string | null>(null);
  const [online, setOnline] = useState(true);
  const [initialLoadDone, setInitialLoadDone] = useState(false);
  const [view, setView] = useState<KioskView>({ name: 'queue' });
  const [busy, setBusy] = useState(false);
  // Server refusal for the over-count correction, rendered INLINE on the
  // correction screen (verbatim, next to the confirm button) — mirrors the crew
  // station's badgeError pattern. A toast alone proved unreadable on the floor.
  const [correctError, setCorrectError] = useState<string | null>(null);
  const [toasts, setToasts] = useState<KioskToast[]>([]);
  const [nowMs, setNowMs] = useState(() => Date.now());
  // Honest timers (decision 8): server_time rides the queue + my-active-job
  // polls; the skew is computed once per poll and corrects clock + cycle timer.
  const [serverSkewMs, setServerSkewMs] = useState(0);
  // NCR filed from a scrap report THIS session — echoed on the complete modal.
  // Keyed by operation so job A's NCR number can never render on job B's
  // complete summary (e.g. scrap→NCR on A, hold A, clock in to B, complete B).
  const [sessionNcr, setSessionNcr] = useState<{ operationId: number; ncrNumber: string } | null>(null);

  const correctedNowMs = nowMs + serverSkewMs;

  // Lean Phase 1: company scrap reason codes for the scrap picker (this kiosk
  // runs on a normal user session, so the /quality read works). Fail-soft: an
  // empty list falls back to the legacy SCRAP_REASONS tiles.
  const { codes: scrapCodes } = useScrapReasonCodes(isAuthenticated);

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
      // Explicit envelope types — the api client returns `any` here, and this
      // is the seam the backend B1–B8 payload blocks ride in on.
      const [queueRes, activeRes]: [KioskWorkCenterQueueResponse, MyActiveJobResponse] = await Promise.all([
        api.getWorkCenterQueue(workCenterId),
        api.getMyActiveJob(),
      ]);
      setQueue(queueRes.queue || []);
      if (queueRes.work_center) setWorkCenter(queueRes.work_center);
      const jobs: ActiveJob[] = activeRes.active_jobs || (activeRes.active_job ? [activeRes.active_job] : []);
      setActiveJob(jobs[0] || null);
      const serverTime = queueRes.server_time || activeRes.server_time;
      const serverMs = serverTime ? Date.parse(serverTime) : NaN;
      if (Number.isFinite(serverMs)) setServerSkewMs(serverMs - Date.now());
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

  // Resolve the station's display name once per login (best effort, fallback
  // when the queue payload predates the work_center block).
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

  // 1s ticker — drives the top-bar clock and the cycle timer.
  useEffect(() => {
    if (!isAuthenticated) return undefined;
    const interval = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [isAuthenticated]);

  // --- Idle auto-logout -----------------------------------------------------
  const handleIdleLogout = useCallback(() => {
    logout();
    setView({ name: 'queue' });
    setQueue([]);
    setActiveJob(null);
    setSessionNcr(null);
  }, [logout]);

  const { countdownSeconds } = useKioskIdleLogout({
    enabled: isAuthenticated,
    timeoutSeconds: idleLogoutSeconds,
    onTimeout: handleIdleLogout,
  });

  // A sign-out can also arrive from OUTSIDE the two explicit paths above (the
  // axios interceptor clears dead tokens without navigating on /kiosk). The
  // next operator must never inherit the previous operator's half-open flow,
  // so any authenticated->signed-out transition resets the station state.
  useEffect(() => {
    if (isAuthenticated) return;
    setView({ name: 'queue' });
    setQueue([]);
    setActiveJob(null);
    setSessionNcr(null);
    setCorrectError(null);
  }, [isAuthenticated]);

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

  /** GOOD-tab entry: additive good delta only. */
  const handleReportGood = useCallback(
    async (job: ActiveJob, good: number) => {
      if (!job.operation_id) return;
      setBusy(true);
      try {
        await api.reportOperationProduction(job.operation_id, {
          quantity_complete_delta: good,
          quantity_scrapped_delta: 0,
          source: KIOSK_SOURCE,
        });
        showToast('success', `Saved ${good} good`);
        setView({ name: 'queue' });
        await refresh();
      } catch (err) {
        // Keep the report modal (and its entered quantity) on failure.
        showToast('error', kioskErrorMessage(err, 'Could not save production. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [refresh, showToast]
  );

  /** SCRAP-tab entry: scrap delta + required reason, optional NCR (decision 5). */
  const handleReportScrap = useCallback(
    async (
      job: ActiveJob,
      scrap: number,
      scrapReason: string | null,
      scrapReasonCodeId: number | null,
      openNcr: boolean,
      ncrDescription: string | null
    ) => {
      if (!job.operation_id) return;
      setBusy(true);
      try {
        const res: unknown = await api.reportOperationProduction(job.operation_id, {
          quantity_complete_delta: 0,
          quantity_scrapped_delta: scrap,
          // Structured scrap reason (same TimeEntry.scrap_reason column clock-out
          // writes). Lean Phase 1: the company scrap CODE id rides along too.
          scrap_reason: scrapReason || undefined,
          scrap_reason_code_id: scrapReasonCodeId != null ? scrapReasonCodeId : undefined,
          ...(openNcr ? { open_ncr: true, ...(ncrDescription ? { ncr_description: ncrDescription } : {}) } : {}),
          source: KIOSK_SOURCE,
        });
        const ncrNumber = (res as { ncr?: { ncr_number?: string } | null })?.ncr?.ncr_number;
        if (ncrNumber) {
          setSessionNcr(job.operation_id != null ? { operationId: job.operation_id, ncrNumber } : null);
          showToast('success', `Saved ${scrap} scrap — ${ncrNumber} filed · Quality notified`);
        } else {
          showToast('success', `Saved ${scrap} scrap`);
        }
        setView({ name: 'queue' });
        await refresh();
      } catch (err) {
        // Keep the report modal (and everything entered) on failure.
        showToast('error', kioskErrorMessage(err, 'Could not save production. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [refresh, showToast]
  );

  const handleCorrectProduction = useCallback(
    async (job: ActiveJob, quantity: number, reason: string) => {
      if (!job.operation_id) return;
      setBusy(true);
      setCorrectError(null);
      try {
        await api.reduceOperationProduction(job.operation_id, {
          quantity_delta: quantity,
          reason,
          source: KIOSK_SOURCE,
        });
        showToast('success', `Removed ${quantity} from ${job.work_order_number || 'this job'}`);
        setView({ name: 'queue' });
        await refresh();
      } catch (err) {
        // Non-optimistic: the count never moved locally. Keep the correction view
        // (entered quantity retained) and render the server's refusal verbatim
        // INLINE on the screen — the primary display (no toast; same pattern as
        // the crew station's badge-panel error).
        setCorrectError(kioskErrorMessage(err, 'Could not correct the count. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [refresh, showToast]
  );

  const handleComplete = useCallback(
    async (
      job: ActiveJob,
      good: number,
      scrap: number,
      scrapReason: string | null,
      scrapReasonCodeId: number | null,
      chainItem: KioskQueueItem | null
    ) => {
      if (!job.operation_id) return;
      setBusy(true);
      let clockedOut = false;
      try {
        // Mirrors ShopFloorSimple's sequencing: close the operator's own labor
        // record first (quantities + scrap reason land on the TimeEntry), then
        // assert operation completion at the target quantity.
        const clockOutRes: unknown = await api.clockOut(job.time_entry_id, {
          quantity_produced: good,
          quantity_scrapped: scrap,
          scrap_reason: scrap > 0 && scrapReason ? scrapReason : undefined,
          scrap_reason_code_id: scrap > 0 && scrapReasonCodeId != null ? scrapReasonCodeId : undefined,
          source: KIOSK_SOURCE,
        });
        clockedOut = true;
        // The clock-out itself succeeded but the server flagged missing required
        // step records (the operation deliberately stays IN_PROGRESS). NOT an
        // error — labor was recorded fine. Skip the completion call the server
        // just told us it would refuse, say so as info, and open the steps view
        // with the outstanding steps inline.
        const pendingSteps = extractClockOutStepsIncomplete(clockOutRes);
        if (pendingSteps && job.operation_id != null) {
          showToast('info', clockedOutStepsMessage(pendingSteps));
          setView({ name: 'steps', operationId: job.operation_id, jobLabel: jobLabel(job), missing: pendingSteps });
          return;
        }
        await api.completeOperation(job.operation_id, {
          quantity_complete: Number(job.quantity_ordered || 0),
          source: KIOSK_SOURCE,
        });
        showToast('success', `Completed ${job.work_order_number}`);
        setSessionNcr(null);
        // Chained next-job start (decision 6): NON-optimistic — attempted only
        // after the complete landed, refusal surfaced verbatim, lands on queue.
        if (chainItem && workCenterId != null) {
          try {
            await api.clockIn({
              work_order_id: chainItem.work_order_id,
              operation_id: chainItem.operation_id,
              work_center_id: workCenterId,
              entry_type: 'run',
              source: KIOSK_SOURCE,
            });
            showToast('success', `Clocked in to ${chainItem.work_order_number}`);
          } catch (err) {
            showToast(
              'error',
              kioskErrorMessage(err, `Could not start ${chainItem.work_order_number}. Pick it from the queue.`)
            );
          }
        }
        setView({ name: 'queue' });
      } catch (err) {
        // STEPS_INCOMPLETE (409): required process steps lack conforming
        // records — render the missing steps INLINE in the steps view (with
        // jump-to-step), not just a toast.
        const missing = extractStepsIncomplete(err);
        const message = missing
          ? stepsIncompleteMessage(missing)
          : kioskErrorMessage(err, 'Could not complete. Try again.');
        // Two-step verb: if the clock-out landed but completion was refused, say so
        // honestly AND keep the backend's gating detail verbatim.
        if (clockedOut) {
          showToast('error', `Clocked out, but completing failed: ${message}`);
          if (missing && job.operation_id != null) {
            setView({ name: 'steps', operationId: job.operation_id, jobLabel: jobLabel(job), missing });
          } else {
            setView({ name: 'queue' });
          }
        } else {
          showToast('error', message);
        }
      } finally {
        setBusy(false);
        await refresh();
      }
    },
    [refresh, showToast, workCenterId]
  );

  const handleHold = useCallback(
    async (job: ActiveJob, category: string, note: string) => {
      if (!job.operation_id) return;
      setBusy(true);
      try {
        await api.holdOperation(job.operation_id, {
          category,
          severity: 'medium',
          // The optional note is SENT whenever non-empty (any category). The
          // backend only files a WorkOrderBlocker when the hold carries a note
          // OR a non-OTHER category, so a note-less "Other" still sends the
          // stub note — every kiosk hold files a blocker.
          ...(note ? { note } : category === 'other' ? { note: 'Other (reported at kiosk)' } : {}),
          source: KIOSK_SOURCE,
        });
        showToast('info', 'Operation placed on hold');
        setView({ name: 'queue' });
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

  const machineCode = workCenter?.code || workCenterCode || (workCenterId != null ? `WC ${workCenterId}` : 'Station');
  const machineDetail = [workCenter?.name, workCenter?.description].filter(Boolean).join(' · ');

  // The active job's queue row carries the server-derived steps chip counts.
  const activeQueueItem =
    activeJob?.operation_id != null ? queue.find((q) => q.operation_id === activeJob.operation_id) : undefined;

  // Next queued (non-active) job on this machine, in server (run) order.
  const nextQueueItem = useMemo(
    () => queue.find((q) => q.operation_id !== activeJob?.operation_id) ?? null,
    [queue, activeJob?.operation_id]
  );

  const operatorInitials = `${(user?.first_name || ' ')[0] || ''}${(user?.last_name || ' ')[0] || ''}`
    .trim()
    .toUpperCase();

  if (workCenterId == null) {
    return (
      <div className="fd-scope-kiosk flex min-h-screen flex-col items-center justify-center bg-fd-canvas px-6 text-center">
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
      <div className="fd-scope-kiosk flex min-h-screen items-center justify-center bg-fd-canvas">
        <p className="font-mono text-xl uppercase tracking-[0.14em] text-fd-mute">Starting kiosk…</p>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <KioskBadgeLogin stationLabel={workCenterCode || stationLabel} onLogin={loginWithEmployeeId} />;
  }

  const overlayOpen = view.name === 'production' || view.name === 'complete' || view.name === 'hold';
  const showChrome = view.name !== 'viewer';

  // --- Authenticated kiosk ----------------------------------------------------
  return (
    <div className="fd-scope-kiosk flex min-h-screen flex-col bg-fd-canvas [background-image:linear-gradient(rgba(36,48,68,0.18)_1px,transparent_1px),linear-gradient(90deg,rgba(36,48,68,0.18)_1px,transparent_1px)] [background-size:28px_28px]">
      {/* Top bar — all signed-in views except the doc viewer (which brings its own) */}
      {showChrome && (
        <header
          className={`sticky top-0 z-30 flex h-14 shrink-0 items-center gap-3 border-b border-fd-line bg-fd-panel px-4 transition-opacity duration-150 min-[1100px]:h-[60px] min-[1100px]:px-6 ${
            overlayOpen ? 'opacity-35' : ''
          }`}
        >
          <span className="shrink-0 font-mono text-sm font-bold uppercase tracking-[0.04em] text-fd-ink">
            {machineCode}
          </span>
          {machineDetail && (
            <span className="hidden max-w-56 truncate font-mono text-[11px] uppercase tracking-[0.08em] text-fd-mute sm:block">
              {machineDetail}
            </span>
          )}
          <span
            data-testid="kiosk-connection"
            className={`inline-flex items-center gap-1.5 rounded-[3px] border px-2 py-1 font-mono text-[11px] font-semibold uppercase tracking-[0.08em] ${
              online ? 'border-fd-green/40 bg-fd-green/10 text-fd-green' : 'border-fd-red/50 bg-fd-red/10 text-fd-red'
            }`}
          >
            <span
              aria-hidden="true"
              className={`h-1.5 w-1.5 rounded-full ${online ? 'bg-fd-green shadow-[0_0_6px_var(--fd-green)]' : 'bg-fd-red'}`}
            />
            {online ? 'Online' : 'Offline'}
          </span>
          <div className="flex-1" />
          <span className="font-mono text-[13px] tabular-nums text-fd-ink">
            {formatCentralTime(correctedNowMs, CLOCK_OPTIONS)}
          </span>
          <div className="hidden h-6 w-px bg-fd-line sm:block" aria-hidden="true" />
          <div className="flex min-w-0 items-center gap-2.5">
            <span
              aria-hidden="true"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[3px] border border-fd-line-bright bg-fd-raised font-mono text-xs font-bold text-fd-blue"
            >
              {operatorInitials || '—'}
            </span>
            <span className="hidden min-w-0 flex-col min-[1100px]:flex">
              <span className="truncate text-[13px] font-semibold text-fd-ink">
                {user?.first_name} {user?.last_name}
              </span>
              <span className="font-mono text-[10px] uppercase tracking-[0.06em] text-fd-mute">
                Badge {user?.employee_id || '—'}
              </span>
            </span>
            <span className="font-mono text-[11px] text-fd-body min-[1100px]:hidden">{user?.employee_id || '—'}</span>
          </div>
          <button
            type="button"
            onClick={handleIdleLogout}
            // Disabled mid-mutation: logging out while a clock-in/out is in flight
            // 401s the retry path and bounces the tablet off /kiosk.
            disabled={busy}
            className="inline-flex h-11 items-center rounded-[3px] border border-fd-line px-3.5 font-mono text-xs font-semibold uppercase tracking-[0.08em] text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
          >
            Log out
          </button>
        </header>
      )}

      {/* Offline banner — also the accessible explanation for the disabled
          mutation buttons (referenced via aria-describedby). */}
      {!online && (
        <div
          role="alert"
          id={OFFLINE_HINT_ID}
          className="border-b border-fd-red bg-fd-red/15 px-5 py-3.5 text-center font-mono text-base font-bold uppercase tracking-[0.06em] text-fd-red"
        >
          Offline — actions are disabled until the connection is restored. Reconnecting…
        </div>
      )}

      {/* Idle-logout countdown toast */}
      {countdownSeconds != null && (
        <div
          role="alert"
          data-testid="kiosk-idle-countdown"
          className="border-b border-fd-amber bg-fd-amber/15 px-5 py-3.5 text-center font-mono text-base font-bold uppercase tracking-[0.06em] text-fd-amber"
        >
          Logging out in {countdownSeconds}s — tap anywhere to stay logged in
        </div>
      )}

      {/* Toasts — full width, plain language, above overlays */}
      <div className="fixed inset-x-0 bottom-0 z-[70] space-y-2 p-3">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role={toast.type === 'error' ? 'alert' : 'status'}
            className={`fd-scope-kiosk flex w-full items-center gap-3 rounded-[4px] border px-5 py-3.5 text-lg font-semibold shadow-[0_12px_40px_rgba(0,0,0,0.5)] ${
              toast.type === 'success'
                ? 'border-fd-green/60 bg-fd-panel text-fd-green'
                : toast.type === 'error'
                  ? 'border-fd-red bg-fd-panel text-fd-red'
                  : 'border-fd-blue/60 bg-fd-panel text-fd-blue'
            }`}
          >
            <span
              aria-hidden="true"
              className={`h-2 w-2 shrink-0 rounded-full ${
                toast.type === 'success' ? 'bg-fd-green' : toast.type === 'error' ? 'bg-fd-red' : 'bg-fd-blue'
              }`}
            />
            <span>{toast.message}</span>
          </div>
        ))}
      </div>

      {view.name === 'viewer' ? (
        <KioskDocViewer
          operationId={view.operationId}
          initialTab={view.initialTab}
          transport={OPERATOR_DOC_TRANSPORT}
          onBack={() => setView({ name: 'queue' })}
        />
      ) : (
        <main
          className={`flex w-full flex-1 flex-col transition-opacity duration-150 ${overlayOpen ? 'opacity-35' : ''}`}
        >
          {(view.name === 'queue' || overlayOpen) && (
            <div className="flex flex-1 flex-col gap-3.5 p-3.5 min-[1100px]:flex-row">
              {/* Running-job panel (or the quiet no-job panel) */}
              <section
                aria-label="Active job"
                className="flex min-w-0 flex-col rounded-[4px] border border-fd-line bg-fd-panel min-[1100px]:flex-[1.55]"
              >
                {activeJob ? (
                  <>
                    {/* Header row */}
                    <div className="flex items-center gap-3 border-b border-fd-line px-4 py-3 min-[1100px]:px-[18px]">
                      <span className="inline-flex items-center gap-1.5 rounded-[3px] border border-fd-green/40 bg-fd-green/10 px-2 py-1 font-mono text-[11px] font-semibold uppercase tracking-[0.08em] text-fd-green">
                        <span
                          aria-hidden="true"
                          className="h-1.5 w-1.5 rounded-full bg-fd-green shadow-[0_0_6px_var(--fd-green)]"
                        />
                        Running
                      </span>
                      <span className="truncate font-mono text-[11px] uppercase tracking-[0.14em] text-fd-mute">
                        Op {activeJob.operation_number ?? '—'} · {activeJob.operation_name || 'Operation'}
                      </span>
                      <div className="flex-1" />
                      <span className="hidden font-mono text-[10px] uppercase tracking-[0.14em] text-fd-mute min-[1100px]:block">
                        Cycle
                      </span>
                      <span
                        data-testid="kiosk-active-timer"
                        className="font-mono text-[22px] font-bold tabular-nums text-fd-green [text-shadow:0_0_14px_rgba(63,185,80,0.35)] min-[1100px]:text-[26px]"
                      >
                        {formatElapsed(activeJob.clock_in, correctedNowMs)}
                      </span>
                    </div>

                    {/* Job row */}
                    <div className="flex items-start gap-4 px-4 pt-4 min-[1100px]:px-[18px]">
                      <div className="min-w-0 flex-1">
                        <div className="font-mono text-[30px] font-bold leading-none tracking-[-0.01em] text-fd-ink min-[1100px]:text-[38px]">
                          {activeJob.work_order_number || '—'}
                        </div>
                        <div className="mt-2 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-sm text-fd-body min-[1100px]:text-base">
                          <span className="font-mono font-semibold text-fd-ink">{activeJob.part_number || '—'}</span>
                          {activeJob.part_name ? <span>· {activeJob.part_name}</span> : null}
                          {activeJob.part_revision ? (
                            <span className="rounded-[3px] border border-fd-line px-1.5 py-0.5 font-mono text-xs uppercase text-fd-mute">
                              Rev {activeJob.part_revision}
                            </span>
                          ) : null}
                        </div>
                      </div>
                      <div className="shrink-0 text-right">
                        <div className="font-mono text-[26px] font-bold leading-none tabular-nums text-fd-ink min-[1100px]:text-[34px]">
                          {Number(activeJob.quantity_complete || 0)}
                          <span className="text-[17px] font-medium text-fd-mute min-[1100px]:text-[22px]">
                            {' '}
                            / {Number(activeJob.quantity_ordered || 0)}
                          </span>
                        </div>
                        <div className="mt-1.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fd-mute">
                          Pcs good ·{' '}
                          {Number(activeJob.quantity_ordered || 0) > 0
                            ? Math.min(
                                100,
                                Math.round(
                                  (Number(activeJob.quantity_complete || 0) /
                                    Number(activeJob.quantity_ordered || 1)) *
                                    100
                                )
                              )
                            : 0}
                          %
                        </div>
                      </div>
                    </div>

                    {/* Progress bar */}
                    <div className="px-4 pt-3.5 min-[1100px]:px-[18px]">
                      <div className="h-2 overflow-hidden rounded-[2px] border border-fd-line bg-fd-sunken min-[1100px]:h-2.5">
                        <div
                          className="h-full bg-fd-green shadow-[0_0_10px_rgba(63,185,80,0.4)]"
                          style={{
                            width: `${
                              Number(activeJob.quantity_ordered || 0) > 0
                                ? Math.min(
                                    100,
                                    (Number(activeJob.quantity_complete || 0) /
                                      Number(activeJob.quantity_ordered || 1)) *
                                      100
                                  )
                                : 0
                            }%`,
                          }}
                        />
                      </div>
                    </div>

                    {/* Nest strip */}
                    {activeJob.laser_nest && (
                      <div className="mx-4 mt-4 flex items-center gap-3 rounded-[4px] border border-fd-line bg-fd-raised px-3.5 py-3 min-[1100px]:mx-[18px]">
                        <div className="min-w-0 flex-1">
                          <div className="truncate font-mono text-sm font-semibold uppercase text-fd-ink">
                            {activeJob.laser_nest.cnc_number
                              ? `CNC# ${activeJob.laser_nest.cnc_number}`
                              : activeJob.laser_nest.nest_name}
                            {activeJob.laser_nest.cnc_number &&
                            activeJob.laser_nest.nest_name &&
                            activeJob.laser_nest.nest_name !== activeJob.laser_nest.cnc_number ? (
                              <span className="font-normal text-fd-mute"> · {activeJob.laser_nest.nest_name}</span>
                            ) : null}
                          </div>
                          {(activeJob.laser_nest.material ||
                            activeJob.laser_nest.thickness ||
                            activeJob.laser_nest.sheet_size) && (
                            <div className="mt-1 truncate font-mono text-xs uppercase text-fd-body">
                              {[
                                activeJob.laser_nest.material,
                                activeJob.laser_nest.thickness,
                                activeJob.laser_nest.sheet_size ? `Sheet ${activeJob.laser_nest.sheet_size}` : null,
                              ]
                                .filter(Boolean)
                                .join(' · ')}
                            </div>
                          )}
                        </div>
                        <div className="shrink-0 text-right">
                          <div className="font-mono text-xl font-bold tabular-nums text-fd-ink">
                            {Number(activeJob.laser_nest.completed_runs)}
                            <span className="font-normal text-fd-mute"> / {Number(activeJob.laser_nest.planned_runs)}</span>
                          </div>
                          <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-fd-mute">
                            Sheet runs
                          </div>
                        </div>
                        {activeJob.operation_id != null && (
                          <button
                            type="button"
                            onClick={() =>
                              setView({
                                name: 'viewer',
                                operationId: activeJob.operation_id as number,
                                initialTab: 'nest',
                              })
                            }
                            className="inline-flex h-11 shrink-0 items-center rounded-[3px] border border-fd-blue/40 bg-fd-blue/10 px-3.5 font-mono text-xs font-semibold uppercase tracking-[0.08em] text-fd-blue transition-transform duration-150 ease-out active:scale-[0.98]"
                          >
                            View nest
                          </button>
                        )}
                      </div>
                    )}

                    {/* Process-steps row + SCRAP/NCR */}
                    <div className="mx-4 mt-3 flex gap-3 min-[1100px]:mx-[18px]">
                      {activeJob.operation_id != null && Number(activeQueueItem?.steps_total || 0) > 0 && (
                        <button
                          type="button"
                          data-testid="kiosk-active-steps"
                          disabled={busy}
                          onClick={() =>
                            setView({
                              name: 'steps',
                              operationId: activeJob.operation_id as number,
                              jobLabel: jobLabel(activeJob),
                            })
                          }
                          className="flex min-h-11 min-w-0 flex-1 items-center gap-3 rounded-[4px] border border-fd-cyan/30 bg-fd-cyan/5 px-3.5 py-3 text-left transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40"
                        >
                          {Number(activeQueueItem?.steps_total || 0) <= 10 && (
                            <span className="flex shrink-0 gap-1" aria-hidden="true">
                              {Array.from({ length: Number(activeQueueItem?.steps_total || 0) }).map((_, i) => {
                                const recorded = Number(activeQueueItem?.steps_recorded || 0);
                                return (
                                  <span
                                    key={i}
                                    className={`h-3.5 w-3.5 rounded-[2px] ${
                                      i < recorded
                                        ? 'bg-fd-cyan'
                                        : i === recorded
                                          ? 'border border-fd-cyan bg-fd-sunken'
                                          : 'border border-fd-line-bright bg-fd-sunken'
                                    }`}
                                  />
                                );
                              })}
                            </span>
                          )}
                          <span className="truncate font-mono text-xs font-semibold uppercase tracking-[0.08em] text-fd-cyan">
                            Process steps · {Number(activeQueueItem?.steps_recorded || 0)}/
                            {Number(activeQueueItem?.steps_total || 0)} recorded
                          </span>
                        </button>
                      )}
                      <button
                        type="button"
                        data-testid="kiosk-active-scrap"
                        disabled={mutationsBlocked}
                        aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                        onClick={() => setView({ name: 'production', job: activeJob, tab: 'scrap' })}
                        className="min-h-11 shrink-0 rounded-[4px] border border-fd-red/35 bg-fd-red/5 px-4 font-mono text-xs font-semibold uppercase tracking-[0.08em] text-fd-red transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 min-[1100px]:w-[190px]"
                      >
                        Scrap / NCR
                      </button>
                    </div>

                    <div className="min-h-3 flex-1" />

                    {/* Telemetry tiles */}
                    <div className="mx-4 mb-3.5 grid grid-cols-3 gap-2.5 min-[1100px]:mx-[18px] min-[1100px]:grid-cols-4 min-[1100px]:gap-3">
                      <div className="rounded-[4px] border border-fd-line bg-fd-sunken px-3 py-2.5">
                        <div className="font-mono text-[19px] font-bold tabular-nums text-fd-ink">
                          {activeJob.last_report?.at
                            ? formatCentralTime(activeJob.last_report.at, TIME_HM_OPTIONS)
                            : '—'}
                        </div>
                        <div className="mt-1 truncate font-mono text-[9.5px] uppercase tracking-[0.14em] text-fd-mute">
                          Last report{activeJob.last_report ? ` +${Number(activeJob.last_report.good || 0)}` : ''}
                        </div>
                      </div>
                      {(() => {
                        const produced = Number(activeJob.quantity_produced || 0);
                        const elapsedSec = Math.max(0, (correctedNowMs - Date.parse(activeJob.clock_in)) / 1000);
                        const avgSec = produced > 0 && elapsedSec > 0 ? elapsedSec / produced : null;
                        const remaining = Math.max(
                          0,
                          Number(activeJob.quantity_ordered || 0) - Number(activeJob.quantity_complete || 0)
                        );
                        const estFinish =
                          avgSec != null && remaining > 0
                            ? formatCentralTime(correctedNowMs + remaining * avgSec * 1000, TIME_HM_OPTIONS)
                            : null;
                        return (
                          <>
                            <div className="hidden rounded-[4px] border border-fd-line bg-fd-sunken px-3 py-2.5 min-[1100px]:block">
                              <div className="font-mono text-[19px] font-bold tabular-nums text-fd-ink">
                                {avgSec != null ? formatAvgPerPc(avgSec) : '—'}
                              </div>
                              <div className="mt-1 font-mono text-[9.5px] uppercase tracking-[0.14em] text-fd-mute">
                                Avg per pc
                              </div>
                            </div>
                            <div className="rounded-[4px] border border-fd-line bg-fd-sunken px-3 py-2.5">
                              <div className="font-mono text-[19px] font-bold tabular-nums text-fd-ink">
                                {estFinish ?? '—'}
                              </div>
                              <div className="mt-1 font-mono text-[9.5px] uppercase tracking-[0.14em] text-fd-mute">
                                Est op finish
                              </div>
                            </div>
                          </>
                        );
                      })()}
                      <div className="rounded-[4px] border border-fd-line bg-fd-sunken px-3 py-2.5">
                        <div
                          className={`font-mono text-[19px] font-bold tabular-nums ${
                            Number(activeJob.downtime_minutes || 0) > 0 ? 'text-fd-amber' : 'text-fd-ink'
                          }`}
                        >
                          {activeJob.downtime_minutes != null ? formatDowntime(activeJob.downtime_minutes) : '—'}
                        </div>
                        <div className="mt-1 font-mono text-[9.5px] uppercase tracking-[0.14em] text-fd-mute">
                          Downtime
                        </div>
                      </div>
                    </div>

                    {/* Action bar */}
                    <div className="flex flex-col gap-2.5 border-t border-fd-line px-4 py-3.5 min-[1100px]:px-[18px]">
                      <button
                        type="button"
                        disabled={mutationsBlocked}
                        aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                        onClick={() => setView({ name: 'production', job: activeJob, tab: 'good' })}
                        className="flex h-[60px] items-center justify-center gap-2.5 rounded-[4px] bg-fd-blue font-mono text-[15px] font-bold uppercase tracking-[0.1em] text-[#04101f] shadow-[0_0_24px_rgba(47,129,247,0.25)] transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 min-[1100px]:h-[68px] min-[1100px]:text-[17px]"
                      >
                        <PlusIcon className="h-5 w-5" strokeWidth={2} aria-hidden="true" />
                        Report production
                      </button>
                      <div className="flex gap-2.5">
                        <button
                          type="button"
                          disabled={mutationsBlocked}
                          aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                          onClick={() => setView({ name: 'complete', job: activeJob })}
                          className="h-[50px] flex-1 rounded-[4px] border border-fd-green/45 bg-fd-green/10 font-mono text-[13px] font-bold uppercase tracking-[0.1em] text-fd-green transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 min-[1100px]:h-14"
                        >
                          Complete op
                        </button>
                        <button
                          type="button"
                          disabled={mutationsBlocked}
                          aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                          onClick={() => setView({ name: 'hold', job: activeJob })}
                          className="h-[50px] flex-1 rounded-[4px] border border-fd-amber/45 bg-fd-amber/8 font-mono text-[13px] font-bold uppercase tracking-[0.1em] text-fd-amber transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 min-[1100px]:h-14"
                        >
                          Hold
                        </button>
                        <button
                          type="button"
                          data-testid="kiosk-active-correct"
                          disabled={mutationsBlocked}
                          aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                          onClick={() => {
                            setCorrectError(null);
                            setView({ name: 'correct', job: activeJob });
                          }}
                          className="h-[50px] flex-1 rounded-[4px] border border-fd-line font-mono text-[13px] font-semibold uppercase tracking-[0.1em] text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 min-[1100px]:h-14"
                        >
                          Correct count
                        </button>
                      </div>
                    </div>
                  </>
                ) : (
                  <div className="flex min-h-32 flex-1 flex-col items-center justify-center gap-2 px-6 py-10 text-center">
                    <p className="font-mono text-sm font-bold uppercase tracking-[0.18em] text-fd-mute">
                      No active job
                    </p>
                    <p className="text-base text-fd-body">Tap a queued job to clock in.</p>
                  </div>
                )}
              </section>

              {/* Queue column */}
              <section aria-label="Work queue" className="flex min-w-0 flex-1 flex-col gap-2.5">
                <div className="flex items-center gap-2.5 px-1">
                  <h2 className="font-mono text-[11px] font-bold uppercase tracking-[0.18em] text-fd-mute">
                    My queue <span className="font-normal text-fd-faint">· {queue.length} job{queue.length === 1 ? '' : 's'}</span>
                  </h2>
                  <div className="flex-1" />
                  <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-fd-faint">
                    Sorted by run order
                  </span>
                </div>
                {!initialLoadDone ? (
                  <p className="py-10 text-center font-mono text-sm uppercase tracking-[0.14em] text-fd-mute">
                    Loading queue…
                  </p>
                ) : queue.length === 0 ? (
                  <p className="rounded-[4px] border border-fd-line bg-fd-panel py-10 text-center text-lg text-fd-mute">
                    No jobs in this station&apos;s queue.
                  </p>
                ) : (
                  <div className="space-y-2.5">
                    {queue.map((item) => (
                      <KioskQueueCard
                        key={item.operation_id}
                        item={item}
                        active={item.operation_id === activeJob?.operation_id}
                        disabled={mutationsBlocked}
                        onSelect={(it) => setView({ name: 'confirm', item: it })}
                        onOpenPdf={(it) =>
                          setView({ name: 'viewer', operationId: it.operation_id, initialTab: 'nest' })
                        }
                      />
                    ))}
                  </div>
                )}
                <div className="flex-1" />
                <p className="flex items-center justify-center gap-2 py-1.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fd-faint">
                  AS9100D · ISO 9001 · ITAR · Sync{' '}
                  <span className={online ? 'text-fd-green' : 'text-fd-red'}>{online ? 'OK' : '—'}</span>
                </p>
              </section>
            </div>
          )}

          {view.name === 'confirm' && (
            <section aria-label="Confirm clock in" className="mx-auto w-full max-w-2xl px-4 py-5">
              <h2 className="text-3xl font-bold text-fd-ink">Clock in?</h2>
              <div className="mt-4 rounded-[4px] border border-fd-line bg-fd-panel p-6">
                <p className="font-mono text-4xl font-bold text-fd-ink">{view.item.work_order_number}</p>
                <p className="mt-3 text-2xl text-fd-body">
                  <span className="font-mono font-semibold text-fd-ink">{view.item.part_number || '—'}</span>
                  {view.item.part_name ? <span className="text-fd-mute"> · {view.item.part_name}</span> : null}
                  {view.item.part_revision ? (
                    <span className="ml-2 rounded-[3px] border border-fd-line px-1.5 py-0.5 align-middle font-mono text-sm uppercase text-fd-mute">
                      Rev {view.item.part_revision}
                    </span>
                  ) : null}
                </p>
                <p className="mt-1 text-xl text-fd-mute">
                  Op {view.item.operation_number ?? '—'} · {view.item.operation_name || 'Operation'}
                </p>
                <p className="mt-3 font-mono text-2xl tabular-nums text-fd-body">
                  {Number(view.item.quantity_complete || 0)} / {Number(view.item.quantity_ordered || 0)} pcs
                </p>
                {view.item.laser_nest && (
                  <div className="mt-4">
                    <LaserNestOperatorPanel
                      nest={view.item.laser_nest}
                      size="kiosk"
                      fetchNestPdf={operatorNestPdfFetcher(view.item.laser_nest)}
                    />
                  </div>
                )}
              </div>
              {Number(view.item.steps_total || 0) > 0 && (
                <button
                  type="button"
                  data-testid="kiosk-confirm-steps"
                  disabled={busy}
                  onClick={() =>
                    setView({
                      name: 'steps',
                      operationId: view.item.operation_id,
                      jobLabel: `${view.item.work_order_number || '—'} · Op ${view.item.operation_number ?? '—'} ${
                        view.item.operation_name || ''
                      }`.trim(),
                    })
                  }
                  className="mt-3 flex min-h-14 w-full items-center justify-center gap-3 rounded-[4px] border border-fd-cyan/40 bg-fd-cyan/5 px-4 font-mono text-base font-bold uppercase tracking-[0.08em] text-fd-cyan transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40"
                >
                  Review {formatStepsChip(view.item).toLowerCase()}
                </button>
              )}
              <div className="mt-5 grid grid-cols-2 gap-3">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setView({ name: 'queue' })}
                  className="flex min-h-16 items-center justify-center gap-2 rounded-[4px] border border-fd-line bg-fd-sunken font-mono text-lg font-bold uppercase tracking-[0.08em] text-fd-body transition-transform duration-150 ease-out active:scale-[0.98] disabled:opacity-40"
                >
                  <ArrowLeftIcon className="h-5 w-5" aria-hidden="true" />
                  Back
                </button>
                <button
                  type="button"
                  disabled={mutationsBlocked}
                  aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                  title={!online ? 'Offline — clock in is disabled until reconnected' : undefined}
                  onClick={() => void handleClockIn(view.item)}
                  className="min-h-16 rounded-[4px] bg-fd-green font-mono text-xl font-bold uppercase tracking-[0.08em] text-[#04140b] transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {!online ? 'Offline' : busy ? 'Clocking in…' : 'Clock in'}
                </button>
              </div>
            </section>
          )}

          {view.name === 'correct' && (
            <div className="px-4 py-5">
              <KioskCorrectionScreen
                jobLabel={jobLabel(view.job)}
                busy={mutationsBlocked}
                error={correctError}
                onConfirm={(quantity, reason) => void handleCorrectProduction(view.job, quantity, reason)}
                onCancel={() => setView({ name: 'queue' })}
              />
            </div>
          )}

          {view.name === 'steps' && (
            <div className="px-4 py-5">
              <KioskStepsPanel
                operationId={view.operationId}
                jobLabel={view.jobLabel}
                transport={OPERATOR_STEPS_TRANSPORT}
                blocked={mutationsBlocked}
                online={online}
                offlineHintId={!online ? OFFLINE_HINT_ID : undefined}
                missing={view.missing ?? null}
                showToast={showToast}
                onBack={() => setView({ name: 'queue' })}
                onRecorded={refresh}
                onBusyChange={setBusy}
                onQualityHeld={(result) => {
                  // The hold already landed server-side (op ON_HOLD, entries
                  // closed): show the NCR number on a dedicated screen, refresh
                  // the queue underneath — same exit target as the HOLD verb.
                  setView({ name: 'ncrFiled', result, jobLabel: view.jobLabel });
                  void refresh();
                }}
              />
            </div>
          )}

          {view.name === 'ncrFiled' && (
            <div className="px-4 py-5">
              <KioskNcrFiledScreen
                result={view.result}
                jobLabel={view.jobLabel}
                doneLabel="Back to queue"
                onDone={() => setView({ name: 'queue' })}
              />
            </div>
          )}
        </main>
      )}

      {/* Overlays (1c/1d, 1f, 1g) — the state machine still owns which is open */}
      {view.name === 'production' && (
        <KioskReportModal
          workOrderNumber={view.job.work_order_number || '—'}
          operationNumber={view.job.operation_number ?? null}
          reportedGood={Number(view.job.quantity_complete || 0)}
          quantityOrdered={Number(view.job.quantity_ordered || 0)}
          fullNestQuantity={view.job.component_quantity}
          scrapCodes={scrapCodes}
          busy={mutationsBlocked}
          online={online}
          offlineHintId={OFFLINE_HINT_ID}
          initialTab={view.tab}
          onCancel={() => setView({ name: 'queue' })}
          onConfirmGood={(good) => void handleReportGood(view.job, good)}
          onConfirmScrap={(scrap, reason, codeId, openNcr, ncrDescription) =>
            void handleReportScrap(view.job, scrap, reason, codeId, openNcr, ncrDescription)
          }
        />
      )}

      {view.name === 'hold' && (
        <KioskHoldModal
          workOrderNumber={view.job.work_order_number || '—'}
          operationNumber={view.job.operation_number ?? null}
          busy={mutationsBlocked}
          online={online}
          offlineHintId={OFFLINE_HINT_ID}
          onCancel={() => setView({ name: 'queue' })}
          onConfirm={(category, note) => void handleHold(view.job, category, note)}
        />
      )}

      {view.name === 'complete' && (
        <KioskCompleteModal
          job={view.job}
          nowMs={correctedNowMs}
          stepsTotal={activeQueueItem?.steps_total}
          stepsRecorded={activeQueueItem?.steps_recorded}
          nextQueueItem={nextQueueItem}
          machineCode={machineCode}
          sessionNcrNumber={
            sessionNcr != null && sessionNcr.operationId === view.job.operation_id ? sessionNcr.ncrNumber : null
          }
          scrapCodes={scrapCodes}
          busy={mutationsBlocked}
          online={online}
          offlineHintId={OFFLINE_HINT_ID}
          onCancel={() => setView({ name: 'queue' })}
          onSteps={
            view.job.operation_id != null
              ? () =>
                  setView({
                    name: 'steps',
                    operationId: view.job.operation_id as number,
                    jobLabel: jobLabel(view.job),
                  })
              : undefined
          }
          onConfirm={(good, scrap, reason, codeId) =>
            void handleComplete(view.job, good, scrap, reason, codeId, nextQueueItem)
          }
        />
      )}
    </div>
  );
}
