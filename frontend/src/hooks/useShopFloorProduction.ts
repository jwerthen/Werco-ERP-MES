import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../services/api';
import { productionRequestId } from '../utils/productionRequestId';
import { isDefinitiveHttpRefusal } from '../components/kiosk/useOneTapPieces';

export type ShopFloorProductionBody = Omit<Parameters<typeof api.reportOperationProduction>[1], 'request_id'>;
export type ShopFloorCorrectionBody = Parameters<typeof api.reduceOperationProduction>[1];
export type ProductionSavePhase = 'idle' | 'saving' | 'saved' | 'not-saved' | 'not-confirmed';

export interface ShopFloorPendingProduction {
  operationId: number;
  body: ShopFloorProductionBody & { request_id: string };
}

export interface ShopFloorPendingCorrection {
  operationId: number;
  body: ShopFloorCorrectionBody;
  submittedAt: string;
}

interface StoredProduction<TDraft> {
  version: 1;
  drafts: Record<string, TDraft>;
  pending: ShopFloorPendingProduction | null;
  pendingCorrection: ShopFloorPendingCorrection | null;
}

interface ProductionState<TDraft> {
  key: string | null;
  record: StoredProduction<TDraft>;
  phase: ProductionSavePhase;
  message: string;
  operationId: number | null;
}

interface ShopFloorProductionOptions {
  companyId?: number | null;
  operatorId?: number | null;
}

const emptyRecord = <TDraft>(): StoredProduction<TDraft> => ({ version: 1, drafts: {}, pending: null, pendingCorrection: null });
const offlineMessage = 'Offline. Your entry is kept on this device. Reconnect before saving.';
const uncertainMessage = 'Not confirmed. Check the original report before entering more production.';
const correctionUncertainMessage = 'Not confirmed. Review the correction history with your supervisor before continuing. Do not submit the removal again.';

function readState<TDraft>(key: string | null): ProductionState<TDraft> {
  let record = emptyRecord<TDraft>();
  if (key) {
    try {
      const stored = JSON.parse(sessionStorage.getItem(key) || 'null');
      if (stored?.version === 1 && stored.drafts && typeof stored.drafts === 'object') {
        const pending = stored.pending;
        const correction = stored.pendingCorrection;
        record = {
          version: 1,
          drafts: stored.drafts,
          pending: pending && Number.isInteger(pending.operationId) &&
            typeof pending.body?.request_id === 'string' ? pending : null,
          pendingCorrection: correction && Number.isInteger(correction.operationId) &&
            typeof correction.body?.quantity_delta === 'number' && typeof correction.body?.reason === 'string'
            ? correction : null,
        };
      }
    } catch { /* A missing draft must not stop read-only shop-floor work. */ }
  }
  return {
    key, record,
    phase: record.pending || record.pendingCorrection ? 'not-confirmed' : 'idle',
    message: record.pendingCorrection ? correctionUncertainMessage : record.pending ? uncertainMessage : '',
    operationId: record.pendingCorrection?.operationId ?? record.pending?.operationId ?? null,
  };
}

function persist<TDraft>(key: string, record: StoredProduction<TDraft>) {
  sessionStorage.setItem(key, JSON.stringify(record));
}

function errorMessage(error: unknown): string {
  const reason = error as { message?: string; response?: { data?: { detail?: unknown } } };
  return typeof reason?.response?.data?.detail === 'string'
    ? reason.response.data.detail
    : reason?.message || 'Could not save production.';
}

/**
 * One explicit report at a time, with a durable request identity written BEFORE
 * posting. Unknown outcomes can only replay that exact body/ID; a queue refresh
 * or changed total cannot prove whether this operator's report committed.
 *
 * Keep this hook at page level. Its company/operator-scoped, operation-keyed
 * drafts survive closing a modal, reload, and logging back in in the same tab.
 * It never stores a credential or sends automatically on reconnect/re-login.
 */
export function useShopFloorProduction<TDraft = unknown>({ companyId, operatorId }: ShopFloorProductionOptions) {
  const storageKey = companyId && operatorId ? `werco:shop-floor-production:v1:${companyId}:${operatorId}` : null;
  const [storedState, setStoredState] = useState(() => readState<TDraft>(storageKey));
  let state = storedState;
  if (state.key !== storageKey) {
    state = readState<TDraft>(storageKey);
    setStoredState(state);
  }
  const stateRef = useRef(state);
  stateRef.current = state;
  const sendingRef = useRef(false);
  const [online, setOnline] = useState(() => typeof navigator === 'undefined' || navigator.onLine !== false);

  useEffect(() => {
    const update = () => setOnline(navigator.onLine !== false);
    window.addEventListener('online', update);
    window.addEventListener('offline', update);
    return () => {
      window.removeEventListener('online', update);
      window.removeEventListener('offline', update);
    };
  }, []);

  const updateState = useCallback((next: ProductionState<TDraft>) => {
    // An old user's response may arrive after a badge/session change. Persist
    // its evidence under that old user, but never paint it in the new session.
    if (stateRef.current.key !== next.key) return;
    stateRef.current = next;
    setStoredState(next);
  }, []);

  const readDraft = useCallback((operationId: number): TDraft | null =>
    stateRef.current.record.drafts[String(operationId)] ?? null, []);

  const saveDraft = useCallback((operationId: number, draft: TDraft) => {
    const current = stateRef.current;
    if (!current.key) return;
    const next = { ...current, record: { ...current.record, drafts: { ...current.record.drafts, [operationId]: draft } } };
    updateState(next);
    try { persist(current.key, next.record); } catch {
      updateState({ ...next, message: 'This browser could not keep your entry after a reload. Keep this page open.' });
    }
  }, [updateState]);

  const clearDraft = useCallback((operationId: number) => {
    const current = stateRef.current;
    if (!current.key || current.record.pending?.operationId === operationId || current.record.pendingCorrection?.operationId === operationId) return;
    const drafts = { ...current.record.drafts };
    delete drafts[String(operationId)];
    const next = { ...current, record: { ...current.record, drafts } };
    updateState(next);
    try { persist(current.key, next.record); } catch { /* Keep in-memory success; an old receipt remains replay-safe. */ }
  }, [updateState]);

  const send = useCallback(async (operationId: number, body: ShopFloorProductionBody, recovery: boolean) => {
    const current = stateRef.current;
    if (!current.key) throw new Error('Sign in before saving production.');
    if (current.key !== storageKey) throw new Error('The original operator must submit this production report.');
    if (sendingRef.current) throw new Error('This production report is already saving.');
    if (current.record.pendingCorrection) throw new Error(correctionUncertainMessage);
    if (navigator.onLine === false) {
      updateState({ ...current, phase: current.record.pending ? 'not-confirmed' : 'not-saved', message: offlineMessage, operationId });
      throw new Error(offlineMessage);
    }
    const previous = current.record.pending;
    if (previous && !recovery) throw new Error(uncertainMessage);
    if (recovery && !previous) throw new Error('There is no unconfirmed report to check.');
    const report: ShopFloorPendingProduction = previous ?? {
      operationId, body: { ...body, request_id: productionRequestId() },
    };
    const pendingRecord = { ...current.record, pending: report };
    // Fail closed if the receipt cannot survive a reload. Sending without this
    // marker would allow a lost response followed by a fresh, duplicate report.
    try { persist(current.key, pendingRecord); } catch {
      const message = 'Could not keep a recovery record on this device. Production was not sent. Keep this page open and try again.';
      updateState({ ...current, phase: previous ? 'not-confirmed' : 'not-saved', message, operationId: report.operationId });
      throw new Error(message);
    }
    sendingRef.current = true;
    updateState({ ...current, record: pendingRecord, phase: 'saving', message: 'Saving…', operationId: report.operationId });
    try {
      const response = await api.reportOperationProduction(report.operationId, report.body);
      const latest = stateRef.current.key === current.key ? stateRef.current.record : pendingRecord;
      const drafts = { ...latest.drafts };
      delete drafts[String(report.operationId)];
      const record = { ...latest, drafts, pending: null };
      // A stale stored marker is safe: recovery repeats the same receipt, never
      // a new additive report. Do not mislabel a confirmed server save as failed.
      try { persist(current.key, record); } catch { /* Original immutable request stays recoverable. */ }
      updateState({ ...current, record, phase: 'saved', message: 'Saved', operationId: report.operationId });
      return response;
    } catch (error) {
      const reason = error as { status?: number; response?: { status?: number } };
      const definitive = !previous && isDefinitiveHttpRefusal(reason?.status ?? reason?.response?.status);
      const latest = stateRef.current.key === current.key ? stateRef.current.record : pendingRecord;
      const record = { ...latest, pending: definitive ? null : report };
      try { persist(current.key, record); } catch { /* The pre-send marker remains available after re-login. */ }
      updateState({
        ...current, record, phase: definitive ? 'not-saved' : 'not-confirmed',
        message: definitive ? `Not saved. ${errorMessage(error)}` : uncertainMessage,
        operationId: report.operationId,
      });
      throw error;
    } finally {
      sendingRef.current = false;
    }
  }, [storageKey, updateState]);

  // The correction endpoint has no receipt contract. Persist its intent before
  // sending, but NEVER retry it: subtracting a second time would lose production.
  const submitCorrection = useCallback(async (operationId: number, body: ShopFloorCorrectionBody) => {
    const current = stateRef.current;
    if (!current.key) throw new Error('Sign in before saving production.');
    if (current.key !== storageKey) throw new Error('The original operator must submit this correction.');
    if (sendingRef.current) throw new Error('This production report is already saving.');
    if (current.record.pendingCorrection) throw new Error(correctionUncertainMessage);
    if (current.record.pending) throw new Error(uncertainMessage);
    if (navigator.onLine === false) {
      updateState({ ...current, phase: 'not-saved', message: offlineMessage, operationId });
      throw new Error(offlineMessage);
    }
    const correction = { operationId, body: { ...body }, submittedAt: new Date().toISOString() };
    const pendingRecord = { ...current.record, pendingCorrection: correction };
    try { persist(current.key, pendingRecord); } catch {
      const message = 'Could not keep a recovery record on this device. Correction was not sent. Keep this page open and try again.';
      updateState({ ...current, phase: 'not-saved', message, operationId });
      throw new Error(message);
    }
    sendingRef.current = true;
    updateState({ ...current, record: pendingRecord, phase: 'saving', message: 'Saving…', operationId });
    try {
      const response = await api.reduceOperationProduction(operationId, correction.body);
      const latest = stateRef.current.key === current.key ? stateRef.current.record : pendingRecord;
      const drafts = { ...latest.drafts };
      delete drafts[String(operationId)];
      const record = { ...latest, drafts, pendingCorrection: null };
      try { persist(current.key, record); } catch { /* A stale marker conservatively requires review after re-login. */ }
      updateState({ ...current, record, phase: 'saved', message: 'Saved', operationId });
      return response;
    } catch (error) {
      const reason = error as { status?: number; response?: { status?: number } };
      const definitive = isDefinitiveHttpRefusal(reason?.status ?? reason?.response?.status);
      const latest = stateRef.current.key === current.key ? stateRef.current.record : pendingRecord;
      const record = { ...latest, pendingCorrection: definitive ? null : correction };
      try { persist(current.key, record); } catch { /* Original intent stays held for review. */ }
      updateState({
        ...current, record, phase: definitive ? 'not-saved' : 'not-confirmed', operationId,
        message: definitive ? `Not saved. ${errorMessage(error)}` : correctionUncertainMessage,
      });
      throw error;
    } finally { sendingRef.current = false; }
  }, [storageKey, updateState]);

  // Call only after an explicit human acknowledgement that correction HISTORY
  // was reviewed with a supervisor. A refreshed total is not proof of this write.
  const acknowledgeCorrectionReview = useCallback(() => {
    const current = stateRef.current;
    if (!current.key || current.key !== storageKey) throw new Error('The original operator must review this correction.');
    if (sendingRef.current) throw new Error('Wait for the current save to finish.');
    if (navigator.onLine === false) throw new Error('Reconnect before acknowledging the correction review.');
    const correction = current.record.pendingCorrection;
    if (!correction) return;
    const drafts = { ...current.record.drafts };
    delete drafts[String(correction.operationId)];
    const record = { ...current.record, drafts, pendingCorrection: null };
    // Removing the marker must succeed before allowing a new subtraction.
    persist(current.key, record);
    updateState({ ...current, record, phase: 'idle', message: '', operationId: null });
  }, [storageKey, updateState]);

  const submit = useCallback((operationId: number, body: ShopFloorProductionBody) => send(operationId, body, false), [send]);
  const retry = useCallback(() => {
    const pending = stateRef.current.record.pending;
    return pending ? send(pending.operationId, pending.body, true) : Promise.reject(new Error('There is no unconfirmed report to check.'));
  }, [send]);

  return {
    submit, retry, submitCorrection, acknowledgeCorrectionReview, readDraft, saveDraft, clearDraft, online,
    phase: state.phase, message: state.message, operationId: state.operationId,
    unconfirmed: state.record.pending,
    unconfirmedCorrection: state.record.pendingCorrection,
    mutationsBlocked: !online || state.phase === 'saving' || state.record.pending !== null || state.record.pendingCorrection !== null,
  };
}
