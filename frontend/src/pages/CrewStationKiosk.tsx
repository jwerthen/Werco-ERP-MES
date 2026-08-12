/**
 * /kiosk?kiosk=1&station=<id> — crew-station kiosk (multi-operator terminal).
 *
 * Coexists with the single-operator OperatorKiosk (?work_center_id=N). Auth is
 * two-tier, mirroring the visitor tablet + wallboard precedents:
 *  - STATION tier: shared PIN unlocks a scoped `type="kiosk"` token (24h,
 *    sessionStorage via the isolated kioskStationClient — NEVER the global
 *    axios client, whose 401 interceptor would bounce the terminal to /login).
 *  - OPERATOR tier: every badge scan mints a 5-minute `scope="kiosk"` access
 *    token (memory only, never persisted). Labor mutations hit the EXISTING
 *    shop-floor endpoints with that token, so the badge-identified operator —
 *    never the station — is the audit actor.
 *
 * Every verb is server-gated ⇒ NON-optimistic: loading state, reflect only
 * what the server returns, surface rejection `detail` VERBATIM. Idle resets
 * half-entered flows back to the crew board (never locks the station); the
 * station locks only explicitly or on a 401 from a station-authed read.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import {
  ArrowLeftCircleIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  IdentificationIcon,
  LockClosedIcon,
  SignalIcon,
  SignalSlashIcon,
} from '@heroicons/react/24/solid';
import * as kioskClient from '../services/kioskStationClient';
import { KioskApiError } from '../services/kioskStationClient';
import { getKioskStationId } from '../utils/kiosk';
import { useKioskIdleLogout } from '../hooks/useKioskIdleLogout';
import { usePageTitle } from '../hooks/usePageTitle';
import { useWakeLock } from '../hooks/useWakeLock';
import KioskKeypad from '../components/kiosk/KioskKeypad';
import KioskCrewJobCard from '../components/kiosk/KioskCrewJobCard';
import KioskQuantityScreen from '../components/kiosk/KioskQuantityScreen';
import KioskCorrectionScreen from '../components/kiosk/KioskCorrectionScreen';
import KioskReasonGrid from '../components/kiosk/KioskReasonGrid';
import KioskCompleteConfirmModal from '../components/kiosk/KioskCompleteConfirmModal';
import KioskNcrFiledScreen from '../components/kiosk/KioskNcrFiledScreen';
import KioskStepsPanel, { StepsTransport } from '../components/kiosk/KioskStepsPanel';
import KioskDocViewer, { KioskDocTransport } from '../components/kiosk/KioskDocViewer';
import { useBadgeCapture } from '../components/kiosk/useBadgeCapture';
import KioskOneTapLane from '../components/kiosk/KioskOneTapLane';
import { useOneTapPieces } from '../components/kiosk/useOneTapPieces';
import type { OneTapBinding } from '../components/kiosk/useOneTapPieces';
import { addStranded, clearStranded, readStranded } from '../components/kiosk/oneTapStrandedStore';
import type { StrandedOneTapRecord } from '../components/kiosk/oneTapStrandedStore';
import LaserNestOperatorPanel from '../components/laser/LaserNestOperatorPanel';
import {
  HOLD_REASONS,
  KIOSK_SOURCE,
  KioskCrewQueueItem,
  UNKNOWN_OPERATOR_LABEL,
  formatCrewTally,
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
import { formatCentralTime } from '../utils/centralTime';
import type { KioskStationSummary } from '../types/kioskStation';
import type { ScrapReasonCodeOption } from '../types/scrapReason';
import type { MissingStepInfo, QualityHoldResult } from '../types/processSheet';

const POLL_INTERVAL_MS = 10_000;
const PIN_MIN = 4;
const PIN_MAX = 8;
// Idle abandons a half-entered flow back to the crew board (flow reset, NOT a
// station logout) — short because a walked-away verb screen blocks the crew.
const IDLE_FLOW_RESET_S = 90;
const OFFLINE_HINT_ID = 'crew-kiosk-offline-hint';

interface OperatorSession {
  /** 5-minute scope:"kiosk" access token — memory only. */
  token: string;
  user: { id: number; full_name: string; employee_id: string | null };
}

interface OperatorOpenJob {
  time_entry_id: number;
  operation_id?: number;
  work_order_number?: string;
  operation_name?: string;
  work_center_name?: string;
  clock_in?: string;
}

type CrewView =
  | { name: 'board' }
  | { name: 'badgeFirst' }
  | { name: 'job'; operationId: number }
  | { name: 'joinLeave'; operationId: number }
  | {
      name: 'leaveQty';
      jobLabel: string;
      timeEntryId: number;
      operationId: number | null;
      operator: OperatorSession;
    }
  // REPORT PRODUCTION is BADGE-FIRST: the scan gates entry to the quantity
  // screen, and every report made there posts under that operator's token.
  //
  // It used to be the other way round (quantities, then a scan to sign them),
  // which cannot deliver one tap per finished part — a signature after the fact
  // is a second action per piece by construction. The precedent for the flip is
  // already in this file: `stepsSign`→`steps` and `docsSign`→`docs` both
  // badge-gate ENTRY and then write N records under the token, and process-step
  // records are quality records. Attribution is unchanged (nothing is written
  // without a badge-minted operator token); what changes is that the operator
  // learns who they are recording as BEFORE they enter numbers, not after.
  //
  // `resume` carries an entry whose post was refused for a dead token, so the
  // re-scan saves it rather than making the operator key it again.
  | { name: 'productionQty'; operationId: number; operator: OperatorSession }
  | {
      name: 'productionSign';
      operationId: number;
      resume?: { good: number; scrap: number; reason: string | null; reasonCodeId: number | null };
    }
  // Over-count correction (reduce-production): quantity + reason, then a badge
  // signature. The signing operator must have an open clock-in on the op — the
  // server bounds the walk-back to THEIR own recorded evidence (crew-safe).
  | { name: 'correctQty'; operationId: number }
  | { name: 'correctSign'; operationId: number; quantity: number; reason: string }
  | { name: 'completeQty'; operationId: number }
  | { name: 'completeConfirm'; operationId: number; good: number; scrap: number; reason: string | null; reasonCodeId: number | null }
  | { name: 'hold'; operationId: number }
  | { name: 'operatorSheet'; operator: OperatorSession; openJobs: OperatorOpenJob[] }
  // Process steps: a badge scan gates entry so every record is attributed to
  // the badge-identified operator (5-minute token; a 401 mid-flow re-scans).
  | { name: 'stepsSign'; operationId: number; missing?: MissingStepInfo[] | null }
  | { name: 'steps'; operationId: number; operator: OperatorSession; missing?: MissingStepInfo[] | null }
  // Drawing/nest viewer: the doc reads live inside the shop-floor fence and
  // need an OPERATOR (badge) token — the station token is honored only by the
  // queue read + badge mint — so entry is badge-gated exactly like steps.
  | { name: 'docsSign'; operationId: number }
  | { name: 'docs'; operationId: number; operator: OperatorSession }
  // One-tap OOT hold succeeded: NO operationId on purpose — the held op leaves
  // the queue and the ghost-guard must not yank the NCR number off the screen.
  | { name: 'ncrFiled'; result: QualityHoldResult; jobLabel: string };

interface KioskToast {
  id: number;
  type: 'success' | 'error' | 'info';
  message: string;
}

let toastSeq = 0;

function crewJobLabel(item: KioskCrewQueueItem): string {
  return `${item.work_order_number || '—'} · Op ${item.operation_number ?? '—'} ${item.operation_name || ''}`.trim();
}

/**
 * Steps transport bound to a badge-minted OPERATOR token: the station token is
 * honored only by the queue read + badge mint, so even the steps READ needs
 * the operator credential — which is also what attributes every record.
 */
function crewStepsTransport(operatorToken: string): StepsTransport {
  return {
    fetchView: (operationId) => kioskClient.getOperationSteps(operatorToken, operationId),
    createRecord: (operationId, stepId, data) =>
      kioskClient.recordOperationStep(operatorToken, operationId, stepId, data),
    supersedeRecord: (operationId, stepId, recordId, data) =>
      kioskClient.supersedeOperationStepRecord(operatorToken, operationId, stepId, recordId, data),
    uploadAttachment: (operationId, stepId, file) =>
      kioskClient.uploadOperationStepAttachment(operatorToken, operationId, stepId, file),
    // No `source` hint anywhere on this transport: the badge-minted kiosk
    // token is authoritative — the server records "kiosk" regardless.
    qualityHold: (operationId, stepId, data) =>
      kioskClient.raiseStepQualityHold(operatorToken, operationId, stepId, data),
  };
}

/** Doc-viewer transport bound to a badge-minted operator token (never navigates). */
function crewDocTransport(operatorToken: string): KioskDocTransport {
  return {
    fetchOperationDocuments: (operationId) => kioskClient.getOperationDocuments(operatorToken, operationId),
    fetchDocumentBlob: (documentId) => kioskClient.fetchDocumentBlob(operatorToken, documentId),
  };
}

export default function CrewStationKiosk() {
  // Renders outside Layout, so the tab title is set here; keep the screen awake.
  usePageTitle('Crew Station · Werco ERP');
  useWakeLock();

  const location = useLocation();
  const stationId = getKioskStationId(location.search);

  // --- Station session --------------------------------------------------------
  const [hasToken, setHasToken] = useState<boolean>(() => kioskClient.getStationToken() != null);
  const [station, setStation] = useState<KioskStationSummary | null>(() => kioskClient.getStoredStation());

  // PIN keypad state.
  const [pin, setPin] = useState('');
  const [pinSubmitting, setPinSubmitting] = useState(false);
  const [pinError, setPinError] = useState<string | null>(null);

  // --- Live queue state ---------------------------------------------------------
  const [queue, setQueue] = useState<KioskCrewQueueItem[]>([]);
  // Lean Phase 1: ACTIVE scrap reason codes, delivered ON the queue payload
  // (the station/badge tokens cannot reach /quality/scrap-reason-codes — path
  // fence). [] -> the legacy SCRAP_REASONS fallback inside KioskQuantityScreen.
  const [scrapCodes, setScrapCodes] = useState<ScrapReasonCodeOption[]>([]);
  const [serverSkewMs, setServerSkewMs] = useState(0);
  const [online, setOnline] = useState(true);
  const [initialLoadDone, setInitialLoadDone] = useState(false);
  const [view, setView] = useState<CrewView>({ name: 'board' });
  const [busy, setBusy] = useState(false);
  const [holdReason, setHoldReason] = useState<string | null>(null);
  const [joinEntryType, setJoinEntryType] = useState<'run' | 'setup'>('run');
  const [badgeError, setBadgeError] = useState<string | null>(null);
  // Buffer for the board-level scanner (a badge scanned straight at the crew
  // board opens the operator sheet). Cleared on every flow reset so a stray
  // keystroke can't pollute the next scan.
  const [boardBadgeBuffer, setBoardBadgeBuffer] = useState('');
  const [toasts, setToasts] = useState<KioskToast[]>([]);
  const [nowMs, setNowMs] = useState(() => Date.now());

  // Honest timers: server_time anchors the clock so a fast/slow tablet doesn't lie.
  const skewedNowMs = nowMs + serverSkewMs;

  const showToast = useCallback((type: KioskToast['type'], message: string) => {
    const id = ++toastSeq;
    setToasts((prev) => [...prev.slice(-2), { id, type, message }]);
    // Errors must stay readable from arm's length: linger 4x longer.
    window.setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), type === 'error' ? 12_000 : 3_000);
  }, []);

  const lockStation = useCallback((reason?: string) => {
    kioskClient.clearStationToken();
    setHasToken(false);
    setStation(null);
    setQueue([]);
    setScrapCodes([]);
    setInitialLoadDone(false);
    setView({ name: 'board' });
    setPin('');
    setPinError(reason ?? null);
  }, []);

  // --- Queue refresh (10s poll + refetch-after-mutate, stale polls discarded) --
  // The generation counter guards against a stale in-flight poll overwriting the
  // fresher state a mutation just refetched: every mutation bumps the counter,
  // and any response minted under an older generation is discarded.
  const generationRef = useRef(0);
  const workCenterId = station?.work_center_id ?? null;

  const refreshQueue = useCallback(async () => {
    if (workCenterId == null) return;
    const gen = generationRef.current;
    try {
      const res = await kioskClient.getQueue(workCenterId);
      if (gen !== generationRef.current) return; // stale poll — a mutation superseded it
      setQueue(res.queue || []);
      // Active scrap codes ride every poll (absent on a pre-Lean backend -> []
      // -> the legacy fallback grid, never a crash).
      setScrapCodes(Array.isArray(res.scrap_reason_codes) ? res.scrap_reason_codes : []);
      const serverMs = res.server_time ? Date.parse(res.server_time) : NaN;
      if (Number.isFinite(serverMs)) setServerSkewMs(serverMs - Date.now());
      setOnline(true);
      setInitialLoadDone(true);
    } catch (err) {
      if (gen !== generationRef.current) return;
      if (err instanceof KioskApiError && err.status === 401) {
        // Station revoked/expired: the client already dropped the token — back
        // to the PIN screen, never a /login redirect.
        lockStation('Station session expired or revoked. Enter the PIN to unlock.');
        return;
      }
      // Transient failure: keep last-known data and all form state; show OFFLINE.
      setOnline(false);
      setInitialLoadDone(true);
    }
  }, [workCenterId, lockStation]);

  /** Bump the generation (invalidating in-flight polls) and refetch NOW. */
  const bumpAndRefresh = useCallback(async () => {
    generationRef.current += 1;
    await refreshQueue();
  }, [refreshQueue]);

  useEffect(() => {
    if (!hasToken || workCenterId == null) return undefined;
    void refreshQueue();
    const interval = window.setInterval(() => void refreshQueue(), POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [hasToken, workCenterId, refreshQueue]);

  // Single 1s ticker drives every visible timer.
  useEffect(() => {
    if (!hasToken) return undefined;
    const interval = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [hasToken]);

  // --- Idle = flow reset, not logout ------------------------------------------
  const resetToBoard = useCallback(() => {
    setView({ name: 'board' });
    setBadgeError(null);
    setHoldReason(null);
    setJoinEntryType('run');
    setBoardBadgeBuffer('');
  }, []);

  useKioskIdleLogout({
    // Only armed off the board and never mid-request: an in-flight verb must
    // not have its screen yanked away underneath the response.
    enabled: hasToken && view.name !== 'board' && !busy,
    timeoutSeconds: IDLE_FLOW_RESET_S,
    onTimeout: resetToBoard,
  });

  // If the operation a sub-view points at leaves the queue (completed/held from
  // elsewhere), fall back to the board rather than acting on a ghost.
  useEffect(() => {
    if (!initialLoadDone || busy) return;
    const opId = 'operationId' in view ? view.operationId : null;
    if (opId != null && !queue.some((q) => q.operation_id === opId)) {
      resetToBoard();
    }
  }, [queue, view, busy, initialLoadDone, resetToBoard]);

  const mutationsBlocked = busy || !online;

  // --- One-tap +1 on REPORT PRODUCTION -----------------------------------------
  // TWO refs, and the split is the whole attribution guarantee.
  //
  // The CREDENTIAL is a live 5-minute `scope="kiosk"` bearer. It is cleared
  // aggressively — on Lock station and on every flow reset — so an unattended
  // terminal is never sitting on one, and clearing it only ever PARKS a delta
  // (the pieces stay, they just cannot be sent until that operator scans again).
  //
  // The BINDING is who + which operation, and it is NOT a credential. It has to
  // outlive the token, because it is what a queued flush is checked against: the
  // hook stamps it onto the delta at tap time and refuses to post under any
  // other pair. Without that check, a count parked by a 401 and hidden by the
  // 90s idle reset would post under whoever scanned next, on whatever job they
  // opened — crediting another operator's TimeEntry and moving stock on another
  // work order's operation. Both live in refs rather than `view` because a flush
  // fires from teardown paths where the view is already gone.
  const oneTapCredentialRef = useRef<string | null>(null);
  const oneTapBindingRef = useRef<{ operationId: number; binding: OneTapBinding } | null>(null);
  const [oneTapBinding, setOneTapBinding] = useState<OneTapBinding | null>(null);
  // A badge token is good for 5 minutes; a long run outlives it. The first 401
  // PARKS the lane and asks for a re-scan rather than burning the operator's
  // count against a dead credential.
  const [operatorStale, setOperatorStale] = useState(false);

  const oneTap = useOneTapPieces({
    binding: oneTapBinding,
    canPost: online && !operatorStale,
    post: (pieces, { keepalive }) => {
      const token = oneTapCredentialRef.current;
      const bound = oneTapBindingRef.current;
      if (!token || !bound) {
        return Promise.reject(
          new KioskApiError(401, null, 'Badge session ended — scan your badge to save these pieces.')
        );
      }
      return kioskClient
        .reportProduction(
          token,
          bound.operationId,
          { quantity_complete_delta: pieces, quantity_scrapped_delta: 0, source: KIOSK_SOURCE },
          { keepalive }
        )
        .then(() => undefined);
    },
    toMessage: (err) => kioskErrorMessage(err, 'Could not save production. Try again.'),
    // A KioskApiError carries the HTTP status, so the server answered and
    // nothing was written. Anything else — a dropped connection, an aborted
    // keepalive — leaves it unknown whether the row landed, and this endpoint
    // is additive with no idempotency key, so no automatic path may re-send it.
    isAmbiguousFailure: (err) => !(err instanceof KioskApiError),
    onRecorded: (pieces) => {
      showToast(
        'success',
        `${pieces} pc${pieces === 1 ? '' : 's'} recorded${
          oneTapBindingRef.current ? ` by ${oneTapBindingRef.current.binding.label}` : ''
        }`
      );
      void bumpAndRefresh();
    },
    onFailed: (pieces, message, err) => {
      if (err instanceof KioskApiError && err.status === 401) {
        // The badge token died mid-run. RETRY on the lane cannot fix this — it
        // would post against the same expired credential — so take the operator
        // to the one screen that CAN, carrying the count with them (the scan
        // screen shows what is still waiting). Parking without moving them would
        // leave a count on screen with no working way to bank it.
        setOperatorStale(true);
        const bound = oneTapBindingRef.current;
        if (bound) {
          setBadgeError(null);
          setView({ name: 'productionSign', operationId: bound.operationId });
        }
        showToast('error', `Badge session timed out — scan again to save ${pieces} pc${pieces === 1 ? '' : 's'}`);
        return;
      }
      showToast('error', `${pieces} pc${pieces === 1 ? '' : 's'} NOT saved — ${message}`);
    },
    // The kiosk is going away holding pieces it can never send. Write them down
    // — this is their last chance to exist anywhere at all.
    onStranded: (delta) => addStranded('crew', delta),
    blockedMessage: online
      ? 'Scan your badge again to save these pieces.'
      : 'Not saved yet — waiting for the connection.',
  });

  const { flush: flushOneTap, unbanked: oneTapUnbanked } = oneTap;
  const oneTapUnbankedRef = useRef(0);
  oneTapUnbankedRef.current = oneTapUnbanked;

  /**
   * Drop the live bearer token, keeping the binding so a parked delta can still
   * be named and still be banked by the operator it belongs to. Never called
   * with an un-banked delta before a flush has been attempted.
   */
  const releaseOneTapCredential = useCallback(() => {
    oneTapCredentialRef.current = null;
    setOperatorStale(true);
  }, []);

  // Pieces a previous session tapped and could never send. Read once on mount:
  // this is a NOTICE, not a retry queue — nothing here posts, because the
  // operator who made them is usually gone and the endpoint cannot dedupe a
  // request that may already have landed.
  const [stranded, setStranded] = useState<StrandedOneTapRecord[]>(() => readStranded('crew'));
  const dismissStranded = useCallback((key: string) => {
    clearStranded('crew', key);
    setStranded(readStranded('crew'));
  }, []);

  // Leaving the report screen BANKS whatever is still inside the undo window.
  // The tap was the commit; the window is only a way out of it, and walking away
  // is not one — so Cancel, the idle flow-reset, the ghost-guard and the station
  // lock (which all funnel through a view change) post rather than discard.
  const onReportScreen = view.name === 'productionQty';
  const wasOnReportScreen = useRef(false);
  useEffect(() => {
    if (wasOnReportScreen.current && !onReportScreen) {
      // Bank first, then drop the live bearer. Releasing on EVERY exit — Cancel,
      // the idle flow-reset, the ghost-guard, Lock station — is safe precisely
      // because the flow is badge-first: the screen cannot be re-entered without
      // another scan, so the credential has no reason to outlive it, and an
      // unattended terminal is never holding one. A delta the flush could not
      // send simply parks, still named, still its operator's to come back for.
      void flushOneTap();
      releaseOneTapCredential();
    }
    wasOnReportScreen.current = onReportScreen;
  }, [onReportScreen, flushOneTap, releaseOneTapCredential]);

  // --- PIN login ----------------------------------------------------------------
  const submitPin = useCallback(async () => {
    if (stationId == null || pinSubmitting) return;
    if (pin.length < PIN_MIN || pin.length > PIN_MAX) {
      setPinError(`Enter the ${PIN_MIN}–${PIN_MAX} digit station PIN.`);
      return;
    }
    setPinSubmitting(true);
    setPinError(null);
    try {
      const res = await kioskClient.stationLogin(stationId, pin);
      setStation(res.station);
      setHasToken(true);
      setPin('');
      setView({ name: 'board' });
    } catch (err) {
      const message =
        err instanceof KioskApiError ? err.message : 'Could not reach the server. Check the connection and try again.';
      setPinError(message);
      setPin('');
    } finally {
      setPinSubmitting(false);
    }
  }, [stationId, pin, pinSubmitting]);

  // --- Badge flows ----------------------------------------------------------------
  const findItem = useCallback(
    (operationId: number | null): KioskCrewQueueItem | null =>
      operationId == null ? null : queue.find((q) => q.operation_id === operationId) || null,
    [queue]
  );

  /** JOIN as the freshly badge-identified operator (informational elsewhere-check first). */
  const joinJob = useCallback(
    async (operator: OperatorSession, item: KioskCrewQueueItem, entryType: 'run' | 'setup') => {
      // Informational only, never blocking: multi-job clock-in is allowed.
      try {
        const active = await kioskClient.getMyActiveJob(operator.token);
        const elsewhere = (active.active_jobs || []).filter((j) => j.operation_id !== item.operation_id);
        if (elsewhere.length > 0) {
          const first = elsewhere[0];
          showToast(
            'info',
            `${operator.user.full_name} is also clocked in at ${first.work_order_number || 'another job'}${
              first.work_center_name ? ` (${first.work_center_name})` : ''
            } — that job keeps running.`
          );
        }
      } catch {
        // best effort — the banner is informational
      }
      try {
        await kioskClient.clockIn(operator.token, {
          work_order_id: item.work_order_id,
          operation_id: item.operation_id,
          work_center_id: item.work_center_id,
          entry_type: entryType,
          source: KIOSK_SOURCE,
        });
        showToast('success', `${operator.user.full_name} joined ${item.work_order_number}`);
        setView({ name: 'job', operationId: item.operation_id });
      } catch (err) {
        if (err instanceof KioskApiError && err.status === 400) {
          // e.g. "already clocked in" — roster was stale; refresh sorts it out.
          showToast('info', kioskErrorMessage(err, 'Already clocked in.'));
          setView({ name: 'job', operationId: item.operation_id });
        } else {
          throw err;
        }
      } finally {
        await bumpAndRefresh();
      }
    },
    [bumpAndRefresh, showToast]
  );

  /** Job-first badge scan: roster match decides JOIN vs LEAVE. */
  const handleJoinLeaveBadge = useCallback(
    async (badgeId: string) => {
      const item = view.name === 'joinLeave' ? findItem(view.operationId) : null;
      if (!item || mutationsBlocked) return;
      setBusy(true);
      setBadgeError(null);
      try {
        const minted = await kioskClient.mintBadgeToken(badgeId);
        const operator: OperatorSession = { token: minted.access_token, user: minted.user };
        const rosterEntry = (item.roster || []).find((r) => r.user_id === minted.user.id);
        if (rosterEntry) {
          // LEAVE — close their OWN entry, quantities first (0/0 allowed).
          setView({
            name: 'leaveQty',
            jobLabel: crewJobLabel(item),
            timeEntryId: rosterEntry.time_entry_id,
            operationId: item.operation_id,
            operator,
          });
        } else {
          await joinJob(operator, item, joinEntryType);
        }
      } catch (err) {
        // Bad badge / gating rejection — verbatim, stay on the scan screen.
        setBadgeError(kioskErrorMessage(err, 'Could not read that badge. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [view, findItem, mutationsBlocked, joinJob, joinEntryType]
  );

  /** Badge-first (from the board): open the operator sheet. */
  const handleBoardBadge = useCallback(
    async (badgeId: string) => {
      const id = badgeId.trim();
      if (!id || mutationsBlocked) return;
      setBusy(true);
      setBadgeError(null);
      try {
        const minted = await kioskClient.mintBadgeToken(id);
        const operator: OperatorSession = { token: minted.access_token, user: minted.user };
        let openJobs: OperatorOpenJob[] = [];
        try {
          const active = await kioskClient.getMyActiveJob(operator.token);
          openJobs = active.active_jobs || [];
        } catch {
          // sheet still works for joining; open entries just won't list
        }
        setView({ name: 'operatorSheet', operator, openJobs });
      } catch (err) {
        setBadgeError(kioskErrorMessage(err, 'Could not read that badge. Try again.'));
        setView({ name: 'badgeFirst' });
      } finally {
        setBusy(false);
      }
    },
    [mutationsBlocked]
  );

  // Board-level scanner: a badge scanned at the crew board opens the operator
  // sheet directly. Exactly one enabled capture exists at a time — this one is
  // only live on the board (sub-views and the complete modal own it otherwise).
  useBadgeCapture({
    enabled: hasToken && view.name === 'board' && !busy,
    value: boardBadgeBuffer,
    onValueChange: setBoardBadgeBuffer,
    onSubmit: (raw) => {
      setBoardBadgeBuffer('');
      void handleBoardBadge(raw);
    },
  });

  /** LEAVE — clock out the operator's own entry with quantities. */
  const handleLeaveConfirm = useCallback(
    async (good: number, scrap: number, scrapReason: string | null, scrapReasonCodeId: number | null) => {
      if (view.name !== 'leaveQty' || mutationsBlocked) return;
      setBusy(true);
      try {
        const clockOutRes: unknown = await kioskClient.clockOut(view.operator.token, view.timeEntryId, {
          quantity_produced: good,
          quantity_scrapped: scrap,
          scrap_reason: scrap > 0 && scrapReason ? scrapReason : undefined,
          scrap_reason_code_id: scrap > 0 && scrapReasonCodeId != null ? scrapReasonCodeId : undefined,
          source: KIOSK_SOURCE,
        });
        // The clock-out succeeded but the server flagged missing required step
        // records (the operation deliberately stays IN_PROGRESS). NOT an error —
        // labor was recorded fine. Say so as info and take the just-identified
        // operator straight into the steps view (their badge token is fresh).
        const pendingSteps = extractClockOutStepsIncomplete(clockOutRes);
        if (pendingSteps && view.operationId != null) {
          showToast('info', clockedOutStepsMessage(pendingSteps));
          setView({
            name: 'steps',
            operationId: view.operationId,
            operator: view.operator,
            missing: pendingSteps,
          });
        } else if (pendingSteps) {
          // Clock-out from the operator sheet for a job outside this station's
          // queue: no steps view to open here, but still say what's outstanding.
          showToast('info', clockedOutStepsMessage(pendingSteps));
          setView({ name: 'board' });
        } else {
          showToast('success', `${view.operator.user.full_name} clocked out`);
          setView(view.operationId != null ? { name: 'job', operationId: view.operationId } : { name: 'board' });
        }
        await bumpAndRefresh();
      } catch (err) {
        // Keep the quantity screen (and its entered quantities) on failure.
        showToast('error', kioskErrorMessage(err, 'Could not clock out. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [view, mutationsBlocked, showToast, bumpAndRefresh]
  );

  /**
   * REPORT PRODUCTION — the badge scan that OPENS the quantity screen (and, on a
   * `resume`, saves the entry whose post found the previous token expired).
   *
   * Binding the operator here rather than at the end is what makes one tap per
   * finished part possible at all: the screen behind this scan can post on its
   * own, so `+1` records a piece instead of filling a field that still needs a
   * signature. Every write it makes still carries a badge-minted operator token.
   */
  const handleProductionBadge = useCallback(
    async (badgeId: string) => {
      const item = view.name === 'productionSign' ? findItem(view.operationId) : null;
      if (view.name !== 'productionSign' || !item || mutationsBlocked) return;
      const { operationId, resume } = view;
      setBusy(true);
      setBadgeError(null);
      try {
        const minted = await kioskClient.mintBadgeToken(badgeId);
        const operator: OperatorSession = { token: minted.access_token, user: minted.user };
        if (resume) {
          const { good, scrap, reason, reasonCodeId } = resume;
          await kioskClient.reportProduction(operator.token, operationId, {
            quantity_complete_delta: good,
            quantity_scrapped_delta: scrap,
            scrap_reason: scrap > 0 && reason ? reason : undefined,
            scrap_reason_code_id: scrap > 0 && reasonCodeId != null ? reasonCodeId : undefined,
            source: KIOSK_SOURCE,
          });
          const newTally = formatCrewTally({
            quantity_complete: Number(item.quantity_complete || 0) + good,
            quantity_ordered: item.quantity_ordered,
            quantity_scrapped: Number(item.quantity_scrapped || 0) + scrap,
          });
          showToast('success', `Saved by ${minted.user.full_name} — crew total now ${newTally}`);
        }
        // Bind the lane LAST, and only once the token has proved itself on the
        // resume post: a parked one-tap delta un-parks the moment `operatorStale`
        // clears, and it must not do that against a credential that just failed.
        //
        // NOTE what this scan does and does not authorise. It proves the
        // credential is VALID; it says nothing about whose the held pieces are.
        // A delta already stamped with a different (operator, operation) pair is
        // refused by the hook and goes to `orphaned` — un-parking is not
        // adoption, and this scan cannot consent on another operator's behalf.
        const nextBinding: OneTapBinding = {
          key: `user:${minted.user.id}|op:${operationId}`,
          label: `${minted.user.full_name} · ${crewJobLabel(item)}`,
        };
        oneTapCredentialRef.current = operator.token;
        oneTapBindingRef.current = { operationId, binding: nextBinding };
        setOneTapBinding(nextBinding);
        setOperatorStale(false);
        setView({ name: 'productionQty', operationId, operator });
        if (resume) await bumpAndRefresh();
      } catch (err) {
        // Verbatim rejection; a `resume` entry stays in the view state for
        // another scan, and any parked one-tap delta stays parked.
        setBadgeError(kioskErrorMessage(err, 'Could not save production. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [view, findItem, mutationsBlocked, showToast, bumpAndRefresh]
  );

  /**
   * REPORT PRODUCTION — the KEYED entry (keypad / `+5` / `+25` / full nest),
   * posted directly under the screen's already-scanned operator. No second
   * signature screen: the badge that opened the screen is the signature.
   */
  const handleProductionConfirm = useCallback(
    async (good: number, scrap: number, reason: string | null, reasonCodeId: number | null) => {
      const item = view.name === 'productionQty' ? findItem(view.operationId) : null;
      if (view.name !== 'productionQty' || !item || mutationsBlocked) return;
      const { operator, operationId } = view;
      setBusy(true);
      try {
        await kioskClient.reportProduction(operator.token, operationId, {
          quantity_complete_delta: good,
          quantity_scrapped_delta: scrap,
          scrap_reason: scrap > 0 && reason ? reason : undefined,
          scrap_reason_code_id: scrap > 0 && reasonCodeId != null ? reasonCodeId : undefined,
          source: KIOSK_SOURCE,
        });
        const newTally = formatCrewTally({
          quantity_complete: Number(item.quantity_complete || 0) + good,
          quantity_ordered: item.quantity_ordered,
          quantity_scrapped: Number(item.quantity_scrapped || 0) + scrap,
        });
        showToast('success', `Saved by ${operator.user.full_name} — crew total now ${newTally}`);
        setView({ name: 'job', operationId });
        await bumpAndRefresh();
      } catch (err) {
        if (err instanceof KioskApiError && err.status === 401) {
          // The 5-minute badge token expired mid-shift. Carry the entry to the
          // re-scan rather than making the operator key it a second time.
          setBadgeError(null);
          setOperatorStale(true);
          showToast('info', 'Badge session timed out — scan again to save this entry.');
          setView({
            name: 'productionSign',
            operationId,
            resume: { good, scrap, reason, reasonCodeId },
          });
          return;
        }
        // Everything entered stays on screen for another attempt.
        showToast('error', kioskErrorMessage(err, 'Could not save production. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [view, findItem, mutationsBlocked, showToast, bumpAndRefresh]
  );

  /** CORRECT OVER-COUNT — badge-signature scan walks back the entered quantity. */
  const handleCorrectBadge = useCallback(
    async (badgeId: string) => {
      const item = view.name === 'correctSign' ? findItem(view.operationId) : null;
      if (view.name !== 'correctSign' || !item || mutationsBlocked) return;
      const { quantity, reason } = view;
      setBusy(true);
      setBadgeError(null);
      try {
        const minted = await kioskClient.mintBadgeToken(badgeId);
        await kioskClient.reduceProduction(minted.access_token, item.operation_id, {
          quantity_delta: quantity,
          reason,
          source: KIOSK_SOURCE,
        });
        const newTally = formatCrewTally({
          quantity_complete: Math.max(0, Number(item.quantity_complete || 0) - quantity),
          quantity_ordered: item.quantity_ordered,
          quantity_scrapped: item.quantity_scrapped,
        });
        showToast('success', `${minted.user.full_name} removed ${quantity} — crew total now ${newTally}`);
        setView({ name: 'job', operationId: item.operation_id });
        await bumpAndRefresh();
      } catch (err) {
        // Verbatim rejection (e.g. "You can only remove up to the N piece(s)…");
        // quantity + reason stay in the view state for a re-scan by the right badge.
        setBadgeError(kioskErrorMessage(err, 'Could not correct the count. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [view, findItem, mutationsBlocked, showToast, bumpAndRefresh]
  );

  /** COMPLETE — production first (if final pieces entered), then complete-all. */
  const handleCompleteBadge = useCallback(
    async (badgeId: string) => {
      const item = view.name === 'completeConfirm' ? findItem(view.operationId) : null;
      if (view.name !== 'completeConfirm' || !item || mutationsBlocked) return;
      const { good, scrap, reason, reasonCodeId } = view;
      setBusy(true);
      setBadgeError(null);
      let produced = false;
      let minted: Awaited<ReturnType<typeof kioskClient.mintBadgeToken>> | null = null;
      try {
        minted = await kioskClient.mintBadgeToken(badgeId);
        if (good > 0 || scrap > 0) {
          await kioskClient.reportProduction(minted.access_token, item.operation_id, {
            quantity_complete_delta: good,
            quantity_scrapped_delta: scrap,
            scrap_reason: scrap > 0 && reason ? reason : undefined,
            scrap_reason_code_id: scrap > 0 && reasonCodeId != null ? reasonCodeId : undefined,
            source: KIOSK_SOURCE,
          });
          produced = true;
        }
        const res = await kioskClient.completeOperation(minted.access_token, item.operation_id, {
          quantity_complete: Number(item.quantity_ordered || 0),
          source: KIOSK_SOURCE,
        });
        const closed = res.closed_time_entries || [];
        showToast(
          'success',
          closed.length > 0
            ? `Completed ${item.work_order_number} — clocked out ${closed
                .map((c) => c.operator_name ?? UNKNOWN_OPERATOR_LABEL)
                .join(', ')}`
            : `Completed ${item.work_order_number}`
        );
        resetToBoard();
        await bumpAndRefresh();
      } catch (err) {
        // STEPS_INCOMPLETE (409): required process steps lack conforming
        // records — jump into the steps view (the badge that signed the
        // completion attempt carries the recording identity) and render the
        // missing steps inline, not just a toast.
        const missing = extractStepsIncomplete(err);
        if (missing && minted) {
          const stepsMessage = stepsIncompleteMessage(missing);
          showToast(
            'error',
            produced ? `Saved production, but completing failed: ${stepsMessage}` : stepsMessage
          );
          setView({
            name: 'steps',
            operationId: item.operation_id,
            operator: { token: minted.access_token, user: minted.user },
            missing,
          });
          await bumpAndRefresh();
          return;
        }
        const message = kioskErrorMessage(err, 'Could not complete. Try again.');
        if (produced) {
          // Two-step verb: the production report landed but completion was
          // refused — say so honestly AND keep the gating detail verbatim.
          showToast('error', `Saved production, but completing failed: ${message}`);
          setView({ name: 'job', operationId: item.operation_id });
          await bumpAndRefresh();
        } else if (err instanceof KioskApiError && err.status === 409) {
          // Someone else completed/changed it first — verbatim + refresh.
          showToast('error', message);
          setView({ name: 'job', operationId: item.operation_id });
          await bumpAndRefresh();
        } else {
          setBadgeError(message);
        }
      } finally {
        setBusy(false);
      }
    },
    [view, findItem, mutationsBlocked, showToast, bumpAndRefresh, resetToBoard]
  );

  /** STEPS — badge scan gates entry; records are made in the scanned operator's name. */
  const handleStepsBadge = useCallback(
    async (badgeId: string) => {
      if (view.name !== 'stepsSign' || mutationsBlocked) return;
      const { operationId, missing } = view;
      setBusy(true);
      setBadgeError(null);
      try {
        const minted = await kioskClient.mintBadgeToken(badgeId);
        setView({
          name: 'steps',
          operationId,
          operator: { token: minted.access_token, user: minted.user },
          missing: missing ?? null,
        });
      } catch (err) {
        setBadgeError(kioskErrorMessage(err, 'Could not read that badge. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [view, mutationsBlocked]
  );

  /** DOCS — badge scan gates the viewer (the doc reads need an operator token). */
  const handleDocsBadge = useCallback(
    async (badgeId: string) => {
      if (view.name !== 'docsSign' || mutationsBlocked) return;
      const { operationId } = view;
      setBusy(true);
      setBadgeError(null);
      try {
        const minted = await kioskClient.mintBadgeToken(badgeId);
        setView({ name: 'docs', operationId, operator: { token: minted.access_token, user: minted.user } });
      } catch (err) {
        setBadgeError(kioskErrorMessage(err, 'Could not read that badge. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [view, mutationsBlocked]
  );

  /** HOLD — reason first, then badge signature. */
  const handleHoldBadge = useCallback(
    async (badgeId: string) => {
      const item = view.name === 'hold' ? findItem(view.operationId) : null;
      if (!item || !holdReason || mutationsBlocked) return;
      setBusy(true);
      setBadgeError(null);
      try {
        const minted = await kioskClient.mintBadgeToken(badgeId);
        await kioskClient.holdOperation(minted.access_token, item.operation_id, {
          category: holdReason,
          severity: 'medium',
          // The backend only files a WorkOrderBlocker when the hold carries a note
          // OR a non-OTHER category; the kiosk's "Other" tile is category-only, so
          // send a stub note to make sure every kiosk hold files a blocker.
          ...(holdReason === 'other' ? { note: 'Other (reported at kiosk)' } : {}),
          source: KIOSK_SOURCE,
        });
        showToast('info', 'Operation placed on hold');
        setHoldReason(null);
        resetToBoard();
        await bumpAndRefresh();
      } catch (err) {
        setBadgeError(kioskErrorMessage(err, 'Could not place on hold. Try again.'));
      } finally {
        setBusy(false);
      }
    },
    [view, findItem, holdReason, mutationsBlocked, showToast, bumpAndRefresh, resetToBoard]
  );

  // --- Derived render state ---------------------------------------------------
  const stationLabel = useMemo(() => {
    if (station) {
      const wc = station.work_center_code || station.work_center_name;
      return wc ? `${station.label} · ${wc}` : station.label;
    }
    return stationId != null ? `Station #${stationId}` : 'Crew station';
  }, [station, stationId]);

  const viewItem = 'operationId' in view ? findItem(view.operationId) : null;

  /**
   * What is left to record on an operation: its target less what is already
   * recorded — which is also the most GOOD quantity the server will accept for
   * one more entry. COMPLETE pre-fills its final-entry field with it and all
   * three quantity screens bound their quick-add row by it, off this one
   * expression so a pre-fill and a ceiling can never disagree.
   *
   * `quantity_ordered` on a queue row is already the OPERATION target — the
   * server sends `operation_target_quantity(op, wo)`, i.e. `component_quantity`
   * when the operation carries one, else the work order's quantity — so this is
   * the same number both writers behind the quantity screen measure their
   * refusal against (`/production` 400 "Quantity (N) cannot exceed quantity
   * ordered (T)"; clock-out 400 "Quantity produced exceeds quantity ordered").
   * It bounds the quick-add row only; the keypad and the confirm are unchanged,
   * and the server stays the enforcement — the queue is polled, so this figure
   * can be one crewmate's report out of date.
   */
  const remainingOnOperation = (item: KioskCrewQueueItem): number =>
    Math.max(0, Number(item.quantity_ordered || 0) - Number(item.quantity_complete || 0));

  // --- Guard: no station id in the URL -----------------------------------------
  if (stationId == null) {
    return (
      <div className="fd-scope-kiosk flex min-h-screen flex-col items-center justify-center bg-fd-canvas px-6 text-center">
        <ExclamationTriangleIcon className="h-16 w-16 text-fd-amber" />
        <h1 className="mt-4 text-3xl font-bold text-fd-ink">Station not configured</h1>
        <p className="mt-3 max-w-xl text-lg text-fd-body">
          Open this kiosk with a crew-station URL, e.g.{' '}
          <code className="rounded bg-fd-sunken px-2 py-1 font-mono text-fd-cyan">/kiosk?kiosk=1&amp;station=3</code>
        </p>
        <p className="mt-3 max-w-xl text-sm text-fd-mute">
          Create a station and copy its link from Work Centers → Kiosk stations.
        </p>
      </div>
    );
  }

  // --- PIN unlock screen --------------------------------------------------------
  if (!hasToken || station == null) {
    return (
      <div className="fd-scope-kiosk flex min-h-screen flex-col items-center justify-center bg-fd-canvas px-6 py-10">
        <div className="w-full max-w-md">
          <div className="mb-8 text-center">
            <p className="font-mono text-sm uppercase tracking-[0.3em] text-fd-mute">{stationLabel}</p>
            <h1 className="mt-3 flex items-center justify-center gap-3 text-4xl font-bold text-fd-ink">
              <LockClosedIcon className="h-8 w-8 text-fd-blue" aria-hidden="true" />
              Enter station PIN
            </h1>
            <p className="mt-2 text-lg text-fd-body">
              Ask your supervisor for the {PIN_MIN}–{PIN_MAX} digit PIN
            </p>
          </div>

          <div
            data-testid="crew-pin-display"
            className="mb-4 flex min-h-20 items-center justify-center rounded border border-fd-line-bright bg-fd-sunken px-4"
          >
            {pin ? (
              <span className="font-mono text-5xl font-semibold tracking-[0.4em] text-fd-ink">
                {'•'.repeat(pin.length)}
              </span>
            ) : (
              <span className="text-xl text-fd-faint">Enter PIN…</span>
            )}
          </div>

          {pinError && (
            <div
              role="alert"
              className="mb-4 w-full rounded border border-fd-red bg-fd-red/10 px-4 py-4 text-center text-xl font-semibold text-fd-red"
            >
              {pinError}
            </div>
          )}

          <KioskKeypad value={pin} onChange={setPin} maxLength={PIN_MAX} disabled={pinSubmitting} idPrefix="crew-pin-key" />

          <button
            type="button"
            onClick={() => void submitPin()}
            disabled={pinSubmitting || pin.length < PIN_MIN}
            className="mt-4 flex min-h-20 w-full items-center justify-center gap-3 rounded-[4px] bg-fd-blue font-mono text-2xl font-bold uppercase tracking-wider text-[#04101f] transition-transform duration-150 ease-out active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40"
          >
            {pinSubmitting ? 'Unlocking…' : 'Unlock'}
          </button>
        </div>
      </div>
    );
  }

  // --- Unlocked crew station ------------------------------------------------------
  return (
    <div className="fd-scope-kiosk flex min-h-screen flex-col bg-fd-canvas">
      {/* Station header — always visible */}
      <header className="sticky top-0 z-30 border-b border-fd-line bg-fd-panel px-5 py-3">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-4">
          <div className="min-w-0">
            <p className="truncate font-mono text-xl font-bold tracking-tight text-fd-ink">{stationLabel}</p>
            <p className="truncate text-sm text-fd-mute">Crew station — scan your badge to join or leave a job</p>
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
              onClick={() => lockStation()}
              // Disabled mid-mutation: locking while a clock-in/out is in flight
              // 401s the retry path and strands the record.
              disabled={busy}
              className="flex min-h-16 items-center gap-2 rounded border border-fd-line bg-fd-sunken px-4 text-lg font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:cursor-not-allowed disabled:opacity-40"
            >
              <LockClosedIcon className="h-6 w-6" aria-hidden="true" />
              Lock station
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

      {/* Pieces a previous session could never save. Persistent and on the BOARD,
          not tucked inside the report flow: the parked-count banner only ever
          rendered on the scan screen, so the 90-second idle reset used to hide
          the very thing someone needed to act on. Nothing here posts — it names
          the operator and the job so the pieces can be recorded properly, and
          dismissing is an explicit decision about production that is being
          written off. */}
      {stranded.length > 0 && (
        <div className="border-b border-fd-amber bg-fd-amber/10 px-5 py-4" role="alert">
          <div className="mx-auto max-w-5xl space-y-3">
            {stranded.map((record) => (
              <div key={record.key} className="flex flex-wrap items-center justify-between gap-3">
                <p className="min-w-0 font-mono text-lg font-bold text-fd-amber">
                  {record.pieces} pc{record.pieces === 1 ? '' : 's'} were never saved
                  <span className="ml-2 font-semibold text-fd-body">
                    — tapped by {record.label || 'an operator'} at {formatCentralTime(record.at)}. Record them in the
                    office; they are not on the job.
                  </span>
                </p>
                <button
                  type="button"
                  onClick={() => dismissStranded(record.key)}
                  className="min-h-12 shrink-0 rounded border border-fd-line bg-fd-sunken px-4 font-mono text-sm font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright"
                >
                  Dismiss
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Toasts — full width, plain language */}
      <div className="fixed inset-x-0 bottom-0 z-[70] space-y-2 p-3">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            role={toast.type === 'error' ? 'alert' : 'status'}
            className={`flex w-full items-center gap-3 rounded-[4px] border px-5 py-4 text-xl font-semibold shadow-[0_12px_40px_rgba(0,0,0,0.5)] ${
              toast.type === 'success'
                ? 'border-fd-green/60 bg-fd-panel text-fd-green'
                : toast.type === 'error'
                  ? 'border-fd-red bg-fd-panel text-fd-red'
                  : 'border-fd-blue/60 bg-fd-panel text-fd-blue'
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

      {/* DOCS viewer renders full-bleed (it brings its own top bar); everything
          else lives in the standard centered main column. */}
      {view.name === 'docs' && viewItem ? (
        <KioskDocViewer
          operationId={view.operationId}
          initialTab="nest"
          transport={crewDocTransport(view.operator.token)}
          sessionExpiredMessage="Badge session expired — rescan to view"
          onBack={() => setView({ name: 'job', operationId: view.operationId })}
        />
      ) : (
      <main className="mx-auto w-full max-w-5xl flex-1 space-y-5 px-4 py-5">
        {/* CREW BOARD */}
        {view.name === 'board' && (
          <>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="font-mono text-sm font-bold uppercase tracking-[0.25em] text-fd-mute">
                {station.work_center_name || station.work_center_code || 'Work center'} queue · {queue.length} job
                {queue.length === 1 ? '' : 's'}
              </h2>
              <button
                type="button"
                disabled={mutationsBlocked}
                aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                onClick={() => {
                  setBadgeError(null);
                  setView({ name: 'badgeFirst' });
                }}
                className="flex min-h-16 items-center gap-2 rounded border border-fd-blue bg-fd-blue/15 px-4 text-lg font-bold uppercase tracking-wide text-fd-blue transition-colors hover:bg-fd-blue/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <IdentificationIcon className="h-6 w-6" aria-hidden="true" />
                My jobs — scan or type ID
              </button>
            </div>
            {!initialLoadDone ? (
              <p className="py-10 text-center text-xl text-fd-mute">Loading queue…</p>
            ) : queue.length === 0 ? (
              <p className="rounded border border-fd-line bg-fd-panel py-10 text-center text-xl text-fd-mute">
                No jobs in this station&apos;s queue.
              </p>
            ) : (
              <div className="space-y-3">
                {queue.map((item) => (
                  <KioskCrewJobCard
                    key={item.operation_id}
                    item={item}
                    nowMs={skewedNowMs}
                    disabled={busy}
                    onSelect={(it) => {
                      setBadgeError(null);
                      setView({ name: 'job', operationId: it.operation_id });
                    }}
                  />
                ))}
              </div>
            )}
            <p className="text-center text-sm text-fd-faint">
              Tip: scan your badge from this screen at any time to see your jobs.
            </p>
          </>
        )}

        {/* JOB DETAIL — giant tally + roster + verb grid */}
        {view.name === 'job' && viewItem && (
          <section aria-label="Job detail" className="mx-auto w-full max-w-3xl">
            <div className="rounded border border-fd-line bg-fd-panel p-6">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0">
                  <p className="font-mono text-4xl font-bold text-fd-ink">{viewItem.work_order_number}</p>
                  <p className="mt-2 text-2xl text-fd-body">
                    <span className="font-mono font-semibold text-fd-ink">{viewItem.part_number || '—'}</span>
                    {viewItem.part_name ? <span className="text-fd-mute"> · {viewItem.part_name}</span> : null}
                  </p>
                  <p className="mt-1 text-xl text-fd-mute">
                    Op {viewItem.operation_number ?? '—'} · {viewItem.operation_name || 'Operation'}
                  </p>
                </div>
                <div className="text-right">
                  <p data-testid="kiosk-job-tally" className="font-mono text-5xl font-bold tabular-nums text-fd-ink">
                    {formatCrewTally(viewItem)}
                  </p>
                  <p className="mt-1 text-sm uppercase tracking-widest text-fd-faint">crew total</p>
                </div>
              </div>

              {/* Live roster with per-person timers */}
              <div className="mt-5">
                <p className="mb-2 font-mono text-xs font-bold uppercase tracking-[0.25em] text-fd-mute">
                  Clocked in · {(viewItem.roster || []).length}
                </p>
                {(viewItem.roster || []).length === 0 ? (
                  <p className="rounded border border-fd-line bg-fd-sunken px-4 py-3 text-lg text-fd-mute">
                    No one is clocked in yet — scan a badge to join.
                  </p>
                ) : (
                  <ul aria-label="Crew clocked in" className="space-y-2">
                    {(viewItem.roster || []).map((entry) => (
                      <li
                        key={entry.time_entry_id}
                        className="flex items-center justify-between gap-3 rounded border border-fd-green/50 bg-fd-green/10 px-4 py-3"
                      >
                        <span className="flex items-center gap-3 text-xl font-semibold text-fd-ink">
                          {entry.operator_name ?? UNKNOWN_OPERATOR_LABEL}
                          {entry.entry_type === 'setup' && (
                            <span className="rounded border border-fd-amber/60 px-1.5 py-0.5 font-mono text-xs font-bold uppercase tracking-widest text-fd-amber">
                              Setup
                            </span>
                          )}
                        </span>
                        <span className="font-mono text-2xl font-bold tabular-nums text-fd-green">
                          {formatElapsed(entry.clock_in, skewedNowMs)}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {viewItem.laser_nest && (
                <div className="mt-4">
                  {/* Info only — the inline preview would fetch with NO operator
                      token here (pre-badge). The VIEW NEST button below routes
                      through the badge gate into the doc viewer instead, so a
                      nest preview can never 401 its way toward /login. */}
                  <LaserNestOperatorPanel nest={viewItem.laser_nest} size="kiosk" allowPreview={false} />
                  {viewItem.laser_nest.has_document && (
                    <button
                      type="button"
                      data-testid="crew-view-nest"
                      disabled={mutationsBlocked}
                      aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                      onClick={() => {
                        setBadgeError(null);
                        setView({ name: 'docsSign', operationId: viewItem.operation_id });
                      }}
                      className="mt-3 flex min-h-14 w-full items-center justify-center rounded border border-fd-blue/40 bg-fd-blue/10 px-4 font-mono text-base font-bold uppercase tracking-wide text-fd-blue transition-colors hover:bg-fd-blue/20 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      View nest / drawing
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* Verb grid */}
            <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <button
                type="button"
                disabled={mutationsBlocked}
                aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                onClick={() => {
                  setBadgeError(null);
                  setJoinEntryType('run');
                  setView({ name: 'joinLeave', operationId: viewItem.operation_id });
                }}
                className="flex min-h-20 items-center justify-center gap-3 rounded border border-fd-green bg-fd-green/15 px-4 text-xl font-bold uppercase tracking-wide text-fd-green transition-colors hover:bg-fd-green/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <IdentificationIcon className="h-8 w-8 shrink-0" aria-hidden="true" />
                Join / Leave
              </button>
              <button
                type="button"
                disabled={mutationsBlocked}
                aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                onClick={() => {
                  setBadgeError(null);
                  setView({ name: 'productionSign', operationId: viewItem.operation_id });
                }}
                className="min-h-20 rounded border border-fd-blue bg-fd-blue/15 px-4 text-xl font-bold uppercase tracking-wide text-fd-blue transition-colors hover:bg-fd-blue/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Report production
              </button>
              <button
                type="button"
                data-testid="crew-correct-verb"
                disabled={mutationsBlocked}
                aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                onClick={() => {
                  setBadgeError(null);
                  setView({ name: 'correctQty', operationId: viewItem.operation_id });
                }}
                className="min-h-20 rounded border border-fd-line bg-fd-sunken px-4 text-xl font-bold uppercase tracking-wide text-fd-mute transition-colors hover:border-fd-line-bright hover:text-fd-body disabled:cursor-not-allowed disabled:opacity-40"
              >
                Correct over-count
              </button>
              {Number(viewItem.steps_total || 0) > 0 && (
                <button
                  type="button"
                  data-testid="crew-steps-verb"
                  disabled={mutationsBlocked}
                  aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                  onClick={() => {
                    setBadgeError(null);
                    setView({ name: 'stepsSign', operationId: viewItem.operation_id });
                  }}
                  className="min-h-20 rounded border border-fd-cyan bg-fd-cyan/10 px-4 text-xl font-bold uppercase tracking-wide text-fd-cyan transition-colors hover:bg-fd-cyan/20 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {formatStepsChip(viewItem)}
                </button>
              )}
              <button
                type="button"
                disabled={mutationsBlocked}
                aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                onClick={() => setView({ name: 'completeQty', operationId: viewItem.operation_id })}
                className="min-h-20 rounded border border-fd-green bg-fd-green/15 px-4 text-xl font-bold uppercase tracking-wide text-fd-green transition-colors hover:bg-fd-green/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Complete
              </button>
              <button
                type="button"
                disabled={mutationsBlocked}
                aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                onClick={() => {
                  setHoldReason(null);
                  setBadgeError(null);
                  setView({ name: 'hold', operationId: viewItem.operation_id });
                }}
                className="min-h-20 rounded border border-fd-amber bg-fd-amber/15 px-4 text-xl font-bold uppercase tracking-wide text-fd-amber transition-colors hover:bg-fd-amber/25 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Hold
              </button>
            </div>

            <button
              type="button"
              onClick={resetToBoard}
              disabled={busy}
              className="mt-4 flex min-h-16 w-full items-center justify-center gap-2 rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
            >
              <ArrowLeftCircleIcon className="h-7 w-7" aria-hidden="true" />
              Back to jobs
            </button>
          </section>
        )}

        {/* JOIN / LEAVE — badge scan (roster match decides) */}
        {view.name === 'joinLeave' && viewItem && (
          <section aria-label="Join or leave" className="mx-auto w-full max-w-2xl">
            <h2 className="text-3xl font-bold text-fd-ink">Scan badge to join or leave</h2>
            <p className="mt-1 font-mono text-lg text-fd-mute">{crewJobLabel(viewItem)}</p>

            {/* Entry type applies only when JOINing. */}
            <div className="mt-4">
              <p className="mb-2 font-mono text-xs font-bold uppercase tracking-[0.25em] text-fd-mute">
                If joining, clock in as
              </p>
              <div className="grid grid-cols-2 gap-3" role="group" aria-label="Entry type">
                <button
                  type="button"
                  aria-pressed={joinEntryType === 'run'}
                  onClick={() => setJoinEntryType('run')}
                  disabled={busy}
                  className={`min-h-16 rounded border text-xl font-bold uppercase tracking-wide transition-colors disabled:opacity-40 ${
                    joinEntryType === 'run'
                      ? 'border-fd-green bg-fd-green/20 text-fd-green'
                      : 'border-fd-line bg-fd-sunken text-fd-body hover:border-fd-line-bright'
                  }`}
                >
                  Run
                </button>
                <button
                  type="button"
                  aria-pressed={joinEntryType === 'setup'}
                  onClick={() => setJoinEntryType('setup')}
                  disabled={busy}
                  className={`min-h-16 rounded border text-xl font-bold uppercase tracking-wide transition-colors disabled:opacity-40 ${
                    joinEntryType === 'setup'
                      ? 'border-fd-amber bg-fd-amber/20 text-fd-amber'
                      : 'border-fd-line bg-fd-sunken text-fd-body hover:border-fd-line-bright'
                  }`}
                >
                  Setup
                </button>
              </div>
            </div>

            <BadgeScanPanel
              busy={busy}
              blocked={mutationsBlocked}
              offlineHintId={!online ? OFFLINE_HINT_ID : undefined}
              error={badgeError}
              idPrefix="crew-joinleave"
              onBadge={(id) => void handleJoinLeaveBadge(id)}
              onCancel={() => setView({ name: 'job', operationId: viewItem.operation_id })}
            />
          </section>
        )}

        {/* LEAVE — quantities (0/0 allowed). scrapCodes come off the queue
            payload (the scoped tokens can't reach /quality, so the server
            rides the active codes on the station-authed poll); [] falls back
            to the legacy SCRAP_REASONS grid.

            Good counts up from zero here and clock-out measures its refusal
            against the same operation target /production does, so the quick-add
            row is offered — but ONLY when the queue row resolves. LEAVE is also
            reachable from the operator sheet for a job outside this station's
            queue, where there is no row and so no ceiling to bound the taps
            with (the tally banner is absent for exactly the same reason). No
            ceiling, no row. */}
        {view.name === 'leaveQty' && (
          <KioskQuantityScreen
            title={`Clock out — ${view.operator.user.full_name}`}
            jobLabel={view.jobLabel}
            confirmLabel="Clock out"
            requireTotalPositive={false}
            tallyBanner={
              view.operationId != null && findItem(view.operationId)
                ? `CREW TOTAL SO FAR: ${formatCrewTally(findItem(view.operationId) as KioskCrewQueueItem)} — enter only NEW pieces`
                : undefined
            }
            scrapCodes={scrapCodes}
            quickAddCeiling={viewItem ? remainingOnOperation(viewItem) : undefined}
            fullNestQuantity={viewItem?.component_quantity}
            busy={mutationsBlocked}
            onConfirm={(good, scrap, reason, codeId) => void handleLeaveConfirm(good, scrap, reason, codeId)}
            onCancel={() =>
              setView(view.operationId != null ? { name: 'job', operationId: view.operationId } : { name: 'board' })
            }
          />
        )}

        {/* REPORT PRODUCTION, step 1 — the badge that opens the screen.
            Also the recovery point: a `resume` entry is one whose post found the
            5-minute token expired, and a parked one-tap delta un-parks here. */}
        {view.name === 'productionSign' && viewItem && (
          <section aria-label="Scan badge to report production" className="mx-auto w-full max-w-2xl">
            <h2 className="text-3xl font-bold text-fd-ink">
              {view.resume || oneTapUnbanked > 0 ? 'Scan badge to save' : 'Scan badge to report'}
            </h2>
            <p className="mt-1 font-mono text-lg text-fd-mute">{crewJobLabel(viewItem)}</p>
            {view.resume && (
              <p className="mt-4 rounded border border-fd-blue/50 bg-fd-blue/10 px-4 py-3 font-mono text-xl font-bold text-fd-blue">
                Saving: {view.resume.good} good
                {view.resume.scrap > 0 ? ` · ${view.resume.scrap} scrap (${view.resume.reason})` : ''}
              </p>
            )}
            {oneTapUnbanked > 0 && (
              <p
                data-testid="crew-production-parked"
                className="mt-4 rounded border border-fd-amber/60 bg-fd-amber/10 px-4 py-3 font-mono text-xl font-bold text-fd-amber"
              >
                {oneTapUnbanked} tapped pc{oneTapUnbanked === 1 ? '' : 's'} still waiting to be saved
              </p>
            )}
            {!view.resume && oneTapUnbanked === 0 && (
              <p className="mt-3 text-base text-fd-body">
                Every piece you report is recorded in your name. Scan once, then tap <b>+1 piece</b> as each part comes
                off.
              </p>
            )}
            <BadgeScanPanel
              busy={busy}
              blocked={mutationsBlocked}
              offlineHintId={!online ? OFFLINE_HINT_ID : undefined}
              error={badgeError}
              idPrefix="crew-production"
              onBadge={(id) => void handleProductionBadge(id)}
              onCancel={() => setView({ name: 'job', operationId: viewItem.operation_id })}
            />
          </section>
        )}

        {/* REPORT PRODUCTION, step 2 — the quantity screen, bound to the operator
            who just scanned. Two ways to record, deliberately unalike:

              +1 PIECE  posts itself after a short undo window (one tap per
                        finished part — the whole point of the screen).
              keypad /  fills the GOOD field and posts on RECORD, exactly as
              +5 / +25  before.

            The ceiling both of them clamp to is the operation target less what is
            recorded less what this lane has tapped but not yet banked — otherwise
            a pending delta plus a keyed entry could together key a 400. */}
        {view.name === 'productionQty' && viewItem && (
          <KioskQuantityScreen
            title={`Report production — ${view.operator.user.full_name}`}
            jobLabel={crewJobLabel(viewItem)}
            confirmLabel="Record"
            requireTotalPositive
            tallyBanner={`CREW TOTAL SO FAR: ${formatCrewTally(viewItem)} — enter only NEW pieces`}
            scrapCodes={scrapCodes}
            quickAddCeiling={Math.max(0, remainingOnOperation(viewItem) - oneTapUnbanked)}
            fullNestQuantity={viewItem.component_quantity}
            oneTapLane={
              <KioskOneTapLane
                oneTap={oneTap}
                operatorName={view.operator.user.full_name}
                atCeiling={remainingOnOperation(viewItem) - oneTapUnbanked <= 0}
                blocked={busy}
                online={online}
                offlineHintId={OFFLINE_HINT_ID}
              />
            }
            // The lane always commits first, so exactly one mechanism owns the
            // count at a time and RECORD can never race a pending auto-post into
            // two reports for one run of parts.
            confirmLockedLabel={oneTapUnbanked > 0 ? `Recording ${oneTapUnbanked} pcs…` : null}
            busy={mutationsBlocked}
            onConfirm={(good, scrap, reason, codeId) => void handleProductionConfirm(good, scrap, reason, codeId)}
            onCancel={() => setView({ name: 'job', operationId: viewItem.operation_id })}
          />
        )}

        {/* CORRECT OVER-COUNT — quantity + reason, then badge signature. The
            badge that signs must have an open clock-in on this op; the server
            bounds the walk-back to THAT operator's recorded evidence. */}
        {view.name === 'correctQty' && viewItem && (
          <KioskCorrectionScreen
            jobLabel={crewJobLabel(viewItem)}
            tallyBanner={`CREW TOTAL SO FAR: ${formatCrewTally(viewItem)}`}
            busy={mutationsBlocked}
            onConfirm={(quantity, reason) => {
              setBadgeError(null);
              setView({ name: 'correctSign', operationId: viewItem.operation_id, quantity, reason });
            }}
            onCancel={() => setView({ name: 'job', operationId: viewItem.operation_id })}
          />
        )}

        {view.name === 'correctSign' && viewItem && (
          <section aria-label="Sign over-count correction" className="mx-auto w-full max-w-2xl">
            <h2 className="text-3xl font-bold text-fd-ink">Scan badge to correct</h2>
            <p className="mt-1 font-mono text-lg text-fd-mute">{crewJobLabel(viewItem)}</p>
            <p className="mt-4 rounded border border-fd-amber/50 bg-fd-amber/10 px-4 py-3 font-mono text-xl font-bold text-fd-amber">
              Removing: {view.quantity} good ({view.reason})
            </p>
            <p className="mt-3 text-base text-fd-body">
              Scan the badge of the operator whose count this corrects — you can only remove what you recorded.
            </p>
            <BadgeScanPanel
              busy={busy}
              blocked={mutationsBlocked}
              offlineHintId={!online ? OFFLINE_HINT_ID : undefined}
              error={badgeError}
              idPrefix="crew-correct"
              prompt="Scan badge to correct — or type ID"
              onBadge={(id) => void handleCorrectBadge(id)}
              onCancel={() => setView({ name: 'correctQty', operationId: viewItem.operation_id })}
            />
          </section>
        )}

        {/* COMPLETE — final quantities, then the crew-wide confirm modal.
            Final-entry good PRE-FILLS at the ceiling (the same expression, so
            the two can't disagree), which means the row arrives disabled on a
            normal complete and comes alive only after an operator clears the
            field to rebuild a partial count — exactly as it behaves on the
            single-operator kiosk's COMPLETE overlay. Recording MORE than the
            operation target is an office over-count, not a tap here. */}
        {view.name === 'completeQty' && viewItem && (
          <KioskQuantityScreen
            title="Complete job"
            jobLabel={crewJobLabel(viewItem)}
            confirmLabel="Continue"
            initialGood={remainingOnOperation(viewItem)}
            requireTotalPositive={false}
            tallyBanner={`CREW TOTAL SO FAR: ${formatCrewTally(viewItem)} — enter only NEW pieces`}
            scrapCodes={scrapCodes}
            quickAddCeiling={remainingOnOperation(viewItem)}
            fullNestQuantity={viewItem.component_quantity}
            busy={mutationsBlocked}
            onConfirm={(good, scrap, reason, codeId) => {
              setBadgeError(null);
              setView({ name: 'completeConfirm', operationId: viewItem.operation_id, good, scrap, reason, reasonCodeId: codeId });
            }}
            onCancel={() => setView({ name: 'job', operationId: viewItem.operation_id })}
          />
        )}

        {/* HOLD — reason grid + badge signature on one screen */}
        {view.name === 'hold' && viewItem && (
          <section aria-label="Hold job" className="mx-auto w-full max-w-2xl">
            <h2 className="text-3xl font-bold text-fd-ink">Hold job</h2>
            <p className="mt-1 font-mono text-lg text-fd-mute">{crewJobLabel(viewItem)}</p>
            <p className="mt-4 mb-2 text-lg font-semibold text-fd-amber">Why is this job stopping? — required</p>
            <KioskReasonGrid reasons={HOLD_REASONS} selected={holdReason} onSelect={setHoldReason} disabled={mutationsBlocked} tone="amber" />
            {holdReason ? (
              <BadgeScanPanel
                busy={busy}
                blocked={mutationsBlocked}
                offlineHintId={!online ? OFFLINE_HINT_ID : undefined}
                error={badgeError}
                idPrefix="crew-hold"
                prompt="Scan badge to hold — or type ID"
                onBadge={(id) => void handleHoldBadge(id)}
                onCancel={() => {
                  setHoldReason(null);
                  setView({ name: 'job', operationId: viewItem.operation_id });
                }}
              />
            ) : (
              <button
                type="button"
                onClick={() => setView({ name: 'job', operationId: viewItem.operation_id })}
                disabled={busy}
                className="mt-6 min-h-16 w-full rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
              >
                Cancel
              </button>
            )}
          </section>
        )}

        {/* STEPS — badge scan gates entry (records carry the badge identity) */}
        {view.name === 'stepsSign' && viewItem && (
          <section aria-label="Open process steps" className="mx-auto w-full max-w-2xl">
            <h2 className="text-3xl font-bold text-fd-ink">Scan badge to open steps</h2>
            <p className="mt-1 font-mono text-lg text-fd-mute">{crewJobLabel(viewItem)}</p>
            <p className="mt-3 text-lg text-fd-body">
              Step records are made in your name — scan your badge first.
            </p>
            <BadgeScanPanel
              busy={busy}
              blocked={mutationsBlocked}
              offlineHintId={!online ? OFFLINE_HINT_ID : undefined}
              error={badgeError}
              idPrefix="crew-steps"
              onBadge={(id) => void handleStepsBadge(id)}
              onCancel={() => setView({ name: 'job', operationId: view.operationId })}
            />
          </section>
        )}

        {/* DOCS — badge scan gates the drawing/nest viewer (operator-token reads) */}
        {view.name === 'docsSign' && viewItem && (
          <section aria-label="Open documents" className="mx-auto w-full max-w-2xl">
            <h2 className="text-3xl font-bold text-fd-ink">Scan badge to view documents</h2>
            <p className="mt-1 font-mono text-lg text-fd-mute">{crewJobLabel(viewItem)}</p>
            <p className="mt-3 text-lg text-fd-body">
              Drawings and nests are controlled documents — scan your badge to open them.
            </p>
            <BadgeScanPanel
              busy={busy}
              blocked={mutationsBlocked}
              offlineHintId={!online ? OFFLINE_HINT_ID : undefined}
              error={badgeError}
              idPrefix="crew-docs"
              prompt="Scan badge to view — or type ID"
              onBadge={(id) => void handleDocsBadge(id)}
              onCancel={() => setView({ name: 'job', operationId: view.operationId })}
            />
          </section>
        )}

        {/* STEPS — the shared panel bound to the badge-minted operator token */}
        {view.name === 'steps' && viewItem && (
          <KioskStepsPanel
            key={view.operator.token}
            operationId={view.operationId}
            jobLabel={crewJobLabel(viewItem)}
            transport={crewStepsTransport(view.operator.token)}
            blocked={mutationsBlocked}
            online={online}
            offlineHintId={!online ? OFFLINE_HINT_ID : undefined}
            recordingAs={view.operator.user.full_name}
            missing={view.missing ?? null}
            showToast={showToast}
            onBack={() => setView({ name: 'job', operationId: view.operationId })}
            onRecorded={bumpAndRefresh}
            onBusyChange={setBusy}
            onAuthExpired={(message) => {
              setBadgeError(message);
              setView({ name: 'stepsSign', operationId: view.operationId });
            }}
            onQualityHeld={(result) => {
              // The held op leaves the queue on the next refresh, so hand the
              // NCR number to a queue-independent view BEFORE refreshing.
              setView({ name: 'ncrFiled', result, jobLabel: crewJobLabel(viewItem) });
              void bumpAndRefresh();
            }}
          />
        )}

        {/* NCR FILED — one-tap OOT hold confirmation; Done follows the HOLD exit (board) */}
        {view.name === 'ncrFiled' && (
          <KioskNcrFiledScreen
            result={view.result}
            jobLabel={view.jobLabel}
            doneLabel="Back to board"
            onDone={resetToBoard}
          />
        )}

        {/* BADGE-FIRST — typed/scanned from the board */}
        {view.name === 'badgeFirst' && (
          <section aria-label="Scan badge" className="mx-auto w-full max-w-2xl">
            <h2 className="text-3xl font-bold text-fd-ink">Scan badge — or type ID</h2>
            <p className="mt-1 text-lg text-fd-body">See your open jobs, then join or leave from here.</p>
            <BadgeScanPanel
              busy={busy}
              blocked={mutationsBlocked}
              offlineHintId={!online ? OFFLINE_HINT_ID : undefined}
              error={badgeError}
              idPrefix="crew-badgefirst"
              onBadge={(id) => void handleBoardBadge(id)}
              onCancel={resetToBoard}
            />
          </section>
        )}

        {/* OPERATOR SHEET — badge-first: their open entries + joinable jobs */}
        {view.name === 'operatorSheet' && (
          <section aria-label="Your jobs" className="mx-auto w-full max-w-3xl">
            <h2 className="text-3xl font-bold text-fd-ink">{view.operator.user.full_name}</h2>
            <p className="mt-1 text-lg text-fd-mute">
              Badge {view.operator.user.employee_id || '—'} · actions below are recorded in your name
            </p>

            <div className="mt-5">
              <p className="mb-2 font-mono text-xs font-bold uppercase tracking-[0.25em] text-fd-mute">
                Your open jobs · {view.openJobs.length}
              </p>
              {view.openJobs.length === 0 ? (
                <p className="rounded border border-fd-line bg-fd-sunken px-4 py-3 text-lg text-fd-mute">
                  You are not clocked in anywhere.
                </p>
              ) : (
                <ul className="space-y-2">
                  {view.openJobs.map((job) => (
                    <li key={job.time_entry_id}>
                      <button
                        type="button"
                        disabled={mutationsBlocked}
                        aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                        onClick={() =>
                          setView({
                            name: 'leaveQty',
                            jobLabel: `${job.work_order_number || '—'} · ${job.operation_name || 'Operation'}${
                              job.work_center_name ? ` @ ${job.work_center_name}` : ''
                            }`,
                            timeEntryId: job.time_entry_id,
                            operationId:
                              job.operation_id != null && queue.some((q) => q.operation_id === job.operation_id)
                                ? job.operation_id
                                : null,
                            operator: view.operator,
                          })
                        }
                        className="flex w-full items-center justify-between gap-4 rounded border border-fd-green/50 bg-fd-green/10 px-5 py-4 text-left transition-colors hover:bg-fd-green/20 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        <span className="min-w-0">
                          <span className="block font-mono text-2xl font-bold text-fd-ink">
                            {job.work_order_number || '—'}
                          </span>
                          <span className="block truncate text-lg text-fd-body">
                            {job.operation_name || 'Operation'}
                            {job.work_center_name ? ` · ${job.work_center_name}` : ''}
                          </span>
                        </span>
                        <span className="shrink-0 text-right">
                          {job.clock_in && (
                            <span className="block font-mono text-xl font-bold tabular-nums text-fd-green">
                              {formatElapsed(job.clock_in, skewedNowMs)}
                            </span>
                          )}
                          <span className="block text-sm font-bold uppercase tracking-wider text-fd-green">
                            Tap to clock out
                          </span>
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="mt-6">
              <p className="mb-2 font-mono text-xs font-bold uppercase tracking-[0.25em] text-fd-mute">
                Join a job at this station
              </p>
              {queue.filter((q) => !(q.roster || []).some((r) => r.user_id === view.operator.user.id)).length === 0 ? (
                <p className="rounded border border-fd-line bg-fd-sunken px-4 py-3 text-lg text-fd-mute">
                  No other jobs to join here.
                </p>
              ) : (
                <ul className="space-y-2">
                  {queue
                    .filter((q) => !(q.roster || []).some((r) => r.user_id === view.operator.user.id))
                    .map((item) => (
                      <li key={item.operation_id}>
                        <button
                          type="button"
                          disabled={mutationsBlocked}
                          aria-describedby={!online ? OFFLINE_HINT_ID : undefined}
                          onClick={async () => {
                            setBusy(true);
                            try {
                              await joinJob(view.operator, item, 'run');
                            } catch (err) {
                              showToast('error', kioskErrorMessage(err, 'Could not clock in. Try again.'));
                            } finally {
                              setBusy(false);
                            }
                          }}
                          className="flex w-full items-center justify-between gap-4 rounded border border-fd-line-bright bg-fd-raised px-5 py-4 text-left transition-colors hover:bg-fd-panel disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          <span className="min-w-0">
                            <span className="block font-mono text-2xl font-bold text-fd-ink">
                              {item.work_order_number}
                            </span>
                            <span className="block truncate text-lg text-fd-body">
                              Op {item.operation_number ?? '—'} · {item.operation_name || 'Operation'}
                            </span>
                          </span>
                          <span className="shrink-0 text-sm font-bold uppercase tracking-wider text-fd-blue">
                            Tap to join (run)
                          </span>
                        </button>
                      </li>
                    ))}
                </ul>
              )}
            </div>

            <button
              type="button"
              onClick={resetToBoard}
              disabled={busy}
              className="mt-6 flex min-h-16 w-full items-center justify-center gap-2 rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
            >
              <ArrowLeftCircleIcon className="h-7 w-7" aria-hidden="true" />
              Done
            </button>
          </section>
        )}
      </main>
      )}

      {/* COMPLETE confirm — crew-wide, badge-signed, roster re-derived live. */}
      {view.name === 'completeConfirm' && viewItem && (
        <KioskCompleteConfirmModal
          open
          jobLabel={crewJobLabel(viewItem)}
          roster={viewItem.roster || []}
          nowMs={skewedNowMs}
          pendingGood={view.good}
          pendingScrap={view.scrap}
          // Straight off the station-authed queue poll — no new fetch and no
          // operator token. The crew station is path-fenced to shop-floor, so
          // the office tie API (/work-orders/...) is never reachable from here.
          materialTies={viewItem.material_ties}
          operationScrapped={viewItem.quantity_scrapped}
          quantityOrdered={viewItem.quantity_ordered}
          workOrderNumber={viewItem.work_order_number}
          busy={mutationsBlocked}
          error={badgeError}
          onCancel={() => {
            setBadgeError(null);
            setView({ name: 'job', operationId: viewItem.operation_id });
          }}
          onBadge={(id) => void handleCompleteBadge(id)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Badge-signature panel: window-level scan capture + keypad fallback. Only one
// instance is mounted at a time (views are exclusive), so it always owns the
// scanner while visible.
// ---------------------------------------------------------------------------

interface BadgeScanPanelProps {
  busy: boolean;
  /** busy || offline — hard-disables submission (scan or button). */
  blocked: boolean;
  /** id of the offline banner for aria-describedby, when offline. */
  offlineHintId?: string;
  error: string | null;
  idPrefix: string;
  prompt?: string;
  onBadge: (badgeId: string) => void;
  onCancel: () => void;
}

function BadgeScanPanel({
  busy,
  blocked,
  offlineHintId,
  error,
  idPrefix,
  prompt = 'Scan badge — or type ID',
  onBadge,
  onCancel,
}: BadgeScanPanelProps) {
  const [badge, setBadge] = useState('');

  const submit = useCallback(
    (raw: string) => {
      const id = raw.trim();
      if (!id || blocked) return;
      setBadge('');
      onBadge(id);
    },
    [blocked, onBadge]
  );

  useBadgeCapture({
    enabled: !blocked,
    value: badge,
    onValueChange: setBadge,
    onSubmit: submit,
  });

  return (
    <div className="mt-5">
      <p className="text-center font-mono text-sm font-bold uppercase tracking-[0.25em] text-fd-mute">{prompt}</p>

      <div
        data-testid={`${idPrefix}-badge-display`}
        className="mt-2 flex min-h-16 items-center justify-center rounded border border-fd-line-bright bg-fd-sunken px-4"
      >
        {badge ? (
          <span className="font-mono text-3xl font-semibold tracking-[0.2em] text-fd-ink">{badge}</span>
        ) : (
          <span className="flex items-center gap-3 text-fd-faint">
            <IdentificationIcon className="h-7 w-7" aria-hidden="true" />
            <span className="text-lg">{busy ? 'Working…' : 'Waiting for badge…'}</span>
          </span>
        )}
      </div>

      {error && (
        <div
          role="alert"
          className="mt-3 w-full rounded border border-fd-red bg-fd-red/10 px-4 py-3 text-center text-xl font-semibold text-fd-red"
        >
          {error}
        </div>
      )}

      <div className="mt-3">
        <KioskKeypad value={badge} onChange={setBadge} maxLength={32} disabled={blocked} idPrefix={`${idPrefix}-key`} />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3">
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="min-h-16 rounded border border-fd-line bg-fd-sunken text-xl font-bold uppercase tracking-wide text-fd-body transition-colors hover:border-fd-line-bright disabled:opacity-40"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={() => submit(badge)}
          disabled={blocked || !badge.trim()}
          aria-describedby={offlineHintId}
          className="min-h-16 rounded border border-fd-green bg-fd-green/15 text-xl font-bold uppercase tracking-wide text-fd-green transition-colors hover:bg-fd-green/25 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? 'Working…' : 'Confirm'}
        </button>
      </div>
    </div>
  );
}
